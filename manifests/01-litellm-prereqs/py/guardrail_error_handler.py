from fastapi import HTTPException
from litellm.integrations.custom_logger import CustomLogger
import os

MESSAGES = {
    "aws-bedrock-guardrail": "입력 내용이 보안 정책에 따라 차단되었습니다."
}


class GuardrailErrorHandler(CustomLogger):
    async def async_post_call_failure_hook(
        self, request_data, original_exception,
        user_api_key_dict, traceback_str=None,
    ):
        detail = getattr(original_exception, "detail", None)

        if (
            getattr(original_exception, "status_code", None) != 400
            or not isinstance(detail, dict)
            or detail.get("error") != "Violated guardrail policy"
        ):
            return None

        name = detail.get("guardrail_name")

        # 지정한 가드레일의 차단 메시지만 변경한다.
        if name not in MESSAGES:
            return None

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

        message = MESSAGES[name]
        print("[guardrail-result]", {
            "http_status": getattr(original_exception, "status_code", None),
            "guardrail_name": name,
            "checks": list(checks),
            "policies": list(policies),
            "types": list(types),
            "detail": list(detail),
        }, flush=True)

        if "sensitiveInformation" in checks or \
                "sensitiveInformationPolicy" in policies:
            message = "개인정보나 인증정보를 제거한 후 다시 요청해 주세요."

        elif "promptAttack" in checks or "PROMPT_ATTACK" in types:
            message = "프롬프트 조작으로 판단되는 요청이 차단되었습니다."

        elif "contextualGroundingPolicy" in policies:
            message = "참고 자료에서 응답의 근거를 확인할 수 없습니다."

        elif "HATE" in types:
            message = "혐오발언이 감지되어 요청이 차단되었습니다."

        elif "MISCONDUCT" in types:
            message = "주민등록번호 포함안됩니다."

        elif "PROFANITY" in types:
            message = "주민등록번호 포함안됩니다."

        elif "contentFilter" in checks or "contentPolicy" in policies:
            message = "유해한 내용이 감지되어 요청이 차단되었습니다."

        elif "topicPolicy" in policies:
            message = "주민등록번호가 포함되어있습니다. 개인정보를 제거한 후 다시 요청해 주세요."

        return HTTPException(status_code=400, detail=message)


handler = GuardrailErrorHandler(
    s3_bucket_name="s3-litellm-hsbang",
    s3_region_name=os.getenv("AWS_REGION", "us-east-1"),
    s3_path="litellm/request-logs"
)
