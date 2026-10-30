from fastapi import HTTPException
from litellm.proxy.guardrails.guardrail_hooks.bedrock_guardrails import BedrockGuardrail
import os

DEFAULT_MESSAGE = "입력 내용이 보안 정책에 따라 차단되었습니다."


def _collect(detail: dict):

    # print("[dict list] :", list(detail), flush=True)
    # print("[dict values] :", list(detail.values()), flush=True)

    # InvokeGuardrailChecks API 호출 시 응답 구조가 달라서, bedrock_guardrail_checks, assessments를 모두 확인한다.
    checks = {
        v.get("check")
        for v in detail.get("bedrock_guardrail_checks", [])
        if isinstance(v, dict)
    }
    policies = {
        a.get("policy")
        for a in detail.get("assessments", [])
        if isinstance(a, dict)
    }
    types = {
        m.get("type")
        for a in detail.get("assessments", [])
        if isinstance(a, dict)
        for m in a.get("matches", [])
        if isinstance(m, dict) and m.get("action") == "BLOCKED"
    }
    return checks, policies, types


def to_message(detail: dict) -> str:
    checks, policies, types = _collect(detail)

    # print("[guardrail-result] : ", {
    #     "checks": list(checks),
    #     "policies": list(policies),
    #     "types": list(types),
    #     "detail_keys": list(detail),
    #     "detail_values": list(detail.values())
    # }, flush=True)

    if "sensitiveInformation" in checks or "sensitiveInformationPolicy" in policies:
        return "개인정보나 인증정보를 제거한 후 다시 요청해 주세요."
    if "promptAttack" in checks or "PROMPT_ATTACK" in types:
        return "프롬프트 조작으로 판단되는 요청이 차단되었습니다."
    if "contextualGroundingPolicy" in policies:
        return "참고 자료에서 응답의 근거를 확인할 수 없습니다."
    if "HATE" in types:
        return "혐오발언이 감지되어 요청이 차단되었습니다."
    
    # TODO: MISCONDUCT / PROFANITY 문구가 주민등록번호로 되어 있음 - 의도한 것인지 확인
    if "MISCONDUCT" in types:
        return "주민등록번호 포함안됩니다."
    if "PROFANITY" in types:
        return "주민등록번호 포함안됩니다."
    if "contentFilter" in checks or "contentPolicy" in policies:
        return "유해한 내용이 감지되어 요청이 차단되었습니다."
    if "topicPolicy" in policies:
        return "주민등록번호가 포함되어있습니다. 개인정보를 제거한 후 다시 요청해 주세요."
    return DEFAULT_MESSAGE


def _rewrite(e: Exception, guardrail_name) -> Exception:
    """Bedrock 차단 예외면 한국어 메시지로 바꾼 예외를, 아니면 원래 예외를 돌려준다."""
    detail = getattr(e, "detail", None)
    if (
        getattr(e, "status_code", None) != 400
        or not isinstance(detail, dict)
        or detail.get("error") != "Violated guardrail policy"
    ):
        return e

    message = to_message(detail)
    checks, policies, types = _collect(detail)
    print("[guardrail-result]", {
        "guardrail_name": guardrail_name,
        "checks": list(checks),
        "policies": list(policies),
        "types": list(types),
        "detail_keys": list(detail),
        "message": message,
    }, flush=True)
    return HTTPException(status_code=400, detail=message)


class my_bedrockguardrail(BedrockGuardrail):
    """LiteLLM 기본 Bedrock 가드레일에서 차단 메시지만 바꿔서 던진다.

    엔드포인트(/chat/completions, /v1/messages 등)마다 거치는 경로가 달라서
    pre-call 진입점을 모두 감싼다.
    """

    def __init__(self, guardrailIdentifier=None, guardrailVersion=None, **kwargs):
        super().__init__(
            guardrailIdentifier=guardrailIdentifier or os.getenv("BEDROCK_GUARDRAIL_ID"),
            guardrailVersion=guardrailVersion or os.getenv("BEDROCK_GUARDRAIL_VERSION"),
            **kwargs,
        )
    
    # /chat/completions 등 기존 pre_call 경로
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        # 디버그용: 요청 본문(data)과 키 정보가 Pod 로그에 남으므로 기본은 비활성화
        # print("[guardrail-error-handler] async_pre_call_hook", {
        #     "user_api_key_dict": user_api_key_dict,
        #     "cache": cache,
        #     "data": data,
        #     "call_type": call_type,
        # }, flush=True)

        try:
            return await super().async_pre_call_hook(user_api_key_dict, cache, data, call_type)
        except Exception as e:
            raise _rewrite(e, self.guardrail_name) from e

    # /v1/messages 등 통합 가드레일 경로
    async def apply_guardrail(self, inputs, request_data, input_type, logging_obj=None):
        try:
            return await super().apply_guardrail(inputs, request_data, input_type, logging_obj)
        except Exception as e:
            raise _rewrite(e, self.guardrail_name) from e

    # mode: during_call 로 바꿀 때 대비
    async def async_moderation_hook(self, data, user_api_key_dict, call_type):
        try:
            return await super().async_moderation_hook(data, user_api_key_dict, call_type)
        except Exception as e:
            raise _rewrite(e, self.guardrail_name) from e


