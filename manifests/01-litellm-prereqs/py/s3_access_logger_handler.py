from litellm.integrations.s3_v2 import S3Logger
import os


FIELDS = (
    "id", "startTime", "endTime",
    "model", "status",
    "prompt_tokens", "completion_tokens", "total_tokens",
    "response_cost",
)

IDENTITY_FIELDS = (
    "user_api_key_user_id",
    "user_api_key_team_id",
    "user_api_key_alias",
)


class s3_access_logger_handler(S3Logger):
    async def async_log_success_event(
        self, kwargs, response_obj, start_time, end_time,
    ):
        payload = kwargs.get("standard_logging_object")
     
        if not payload:
            return
        # elif payload:
        #     print("[s3-filter] key list :", list(payload), flush=True)
        #     print("[s3-filter] value list :", list(payload.values()), flush=True)

        metadata = payload.get("metadata") or {}

        # 허용한 최상위 필드만 선택
        selected = {key: payload.get(key) for key in FIELDS}

        # metadata 내부도 필요한 항목만 선택
        selected["metadata"] = {
            key: metadata.get(key) for key in IDENTITY_FIELDS
        }
        selected["requester_ip_address"] = (
            payload.get("requester_ip_address")
            or metadata.get("requester_ip_address")
        )

        selected["request.client.host"] = (
            payload.get("request.client.host")
            or metadata.get("request.client.host")
        )

        print("[s3-filter] selected:", selected, flush=True)

        # 필터한 로그를 내장 S3 로거에 전달
        await super().async_log_success_event(
            kwargs={**kwargs, "standard_logging_object": selected},
            response_obj=response_obj,
            start_time=start_time,
            end_time=end_time,
        )


handler = s3_access_logger_handler(
    s3_bucket_name="s3-litellm-hsbang",
    s3_region_name=os.getenv("AWS_REGION", "us-east-1"),
    s3_path="litellm/request-logs"
)