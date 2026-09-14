# gunicorn 설정.
#
# 워커 2개 고정. 3편에서 이 제한이 "느린 요청이 남을 막는" 병목을 드러냈다.
bind = "0.0.0.0:8000"
workers = 2
worker_class = "sync"
timeout = 60

# 접근 로그도 JSON 한 줄로 남긴다. ingress 가 넘겨 준 요청 번호(X-Request-ID)를 함께 적는다.
#   %({x-request-id}i)s — 요청 헤더. 없으면(파드에 직접 온 요청) "-" 가 찍힌다.
#   %(M)s               — 처리 시간(밀리초)
# 키 사이에 공백을 넣지 않았다. Fluentd 의 /healthz 필터가 "path":"/healthz" 모양으로 거른다.
accesslog = "-"
access_log_format = (
    '{"event":"http.access","request_id":"%({x-request-id}i)s",'
    '"method":"%(m)s","path":"%(U)s","status":%(s)s,"ms":%(M)s}'
)
