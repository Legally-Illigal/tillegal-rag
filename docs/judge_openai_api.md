# Judge cho backend qua OpenAI API

Cập nhật 01/11/2026, checkout `Tilligal-judge-agent`, nhánh `feature/judge-agent`.
Luồng mới: **Backend Python -> JudgeAgent -> OpenAI Responses API -> validator -> Judge JSON**.
Không xây HTTP server, không push/merge và không gọi API trả phí trong nhiệm vụ này.

## Lựa chọn tích hợp và file thay đổi

Provider gọi trực tiếp endpoint chính thức `POST https://api.openai.com/v1/responses` bằng HTTPS/Python stdlib,
theo convention provider Anthropic sẵn có. Đã đối chiếu cả tài liệu SDK, nhưng không thêm SDK/dependency:
không có retry mặc định của SDK cần xử lý; implementation chỉ thực hiện một `opener.open()` cho generation,
không retry/fallback hoặc gọi model catalog trước đó. `requirements-judge.txt` ghi rõ không cần thêm package.

| File | Trách nhiệm |
| --- | --- |
| [openai_provider.py](../ai_core/openai_provider.py) | OpenAIProvider, environment/config, strict schema, HTTP, giới hạn, lỗi, telemetry |
| [judge_cli.py](../ai_core/judge_cli.py) | Thêm `--provider openai`; giữ mock mặc định và hai provider cũ |
| [backend_openai.py](../examples/judge/backend_openai.py) | Hàm khởi tạo server và hàm đánh giá bundle thực qua adapter, trả report/telemetry riêng |
| [test_openai_provider.py](../tests/test_openai_provider.py) | Mock HTTP, cấu hình, validation, lỗi, request count, độc lập Node/OAuth và ví dụ backend |
| `requirements-judge.txt` | Không thêm dependency; baseline requirements giữ nguyên |
| `.gitignore` | Cho phép track test mới, không thay các quy tắc credentials cũ |
| Tài liệu này và liên kết trong báo cáo Judge | Hướng dẫn backend, cấu hình, kết quả kiểm thử |

Schema input/output, adapter, SYSTEM_PROMPT/checklist và validator Judge **giữ nguyên**.
BasicRAG, retrieval, EvidenceBuilder, parser, index và các file Node/OAuth không bị thay đổi trong lần tích hợp này.
Luồng `--provider chatgpt` vẫn dùng đăng nhập cá nhân qua Node/DPAPI; `--provider anthropic` vẫn hoạt động như trước.
Luồng `--provider openai` dùng API key của server, không import/call ChatGPT provider, không cần Node hoặc Windows DPAPI.

## Cấu hình, model và Structured Outputs

Hai biến môi trường server bắt buộc:

| Biến | Ý nghĩa |
| --- | --- |
| `OPENAI_API_KEY` | API key riêng cho server; chỉ dùng trong HTTPS Authorization header |
| `OPENAI_MODEL` | API model ID được project/account cấp quyền, hỗ trợ Responses API và Structured Outputs |

Không có model mặc định hard-code hoặc suy từ tên model hiển thị trong ChatGPT. Tra [API model catalog](https://developers.openai.com/api/docs/models),
mở trang model để kiểm tra Responses/Structured Outputs và giới hạn; dùng ID đúng của API mà project được phép truy cập.
CLI `--model` có thể ghi đè OPENAI_MODEL một cách tường minh. Không có cơ chế tự chọn model khác khi thất bại.

Payload có `text.format={type:json_schema,name:tillegal_judge_v1,strict:true,schema:...}`,
`max_output_tokens`, `store:false`, `stream:false`, `truncation:disabled`.
Không gửi temperature, reasoning, top_p, tools hoặc max_tokens theo Chat Completions.
Provider không duy trì bảng đoán capability theo prefix tên model: model đã chọn phải đáp ứng contract trên;
nếu API từ chối tham số/schema/model thì báo lỗi, không tự bỏ Structured Outputs để chạy tiếp.

Wire schema là projection từ **OUTPUT_SCHEMA của Judge**, giữ nguyên fields/required/types/enums và additionalProperties=false,
không phải DRAFT_OUTPUT_SCHEMA. Các ràng buộc độ dài/regex/số lượng/uniqueItems được bỏ khỏi wire schema để dùng
tập cấu trúc bảo thủ; chúng vẫn được validator gốc kiểm tra sau response, cùng membership ID và status/coverage.
Đây không phải thay đổi contract Judge hoặc sửa output sai cho thành hợp lệ.

## Chạy Windows PowerShell

Module API chỉ cần Python stdlib; không cần pip/npm install hoặc Login ChatGPT. Runtime .tmp dưới đây đã có trong checkout này;
backend triển khai bằng Python của server. requirements-judge.txt không thêm package và không thay requirements ML của baseline.

```powershell
cd 'D:\Trang\TilLegal (Legal AI)\Tilligal-judge-agent'
$py = '.\.tmp\python314\python.exe'

# Kiểm tra offline bằng response fixture: không gọi API, không cần key/model.
& $py -B -m ai_core.judge_cli evaluate --input examples/judge/input.json --provider openai --mock-response examples/judge/sufficient.json

# Test riêng OpenAI provider: tất cả HTTP được mock.
& $py -B -m unittest tests.test_openai_provider -v
```

Lệnh live **chưa được chạy trong nhiệm vụ này**, có phát sinh usage API:

```powershell
$env:OPENAI_MODEL = Read-Host 'API model ID da duoc project cap quyen'
$judgeOpenAISecret = Read-Host 'OpenAI API key cua server' -AsSecureString
$env:OPENAI_API_KEY = [System.Net.NetworkCredential]::new('', $judgeOpenAISecret).Password
try {
    & $py -B -m ai_core.judge_cli evaluate --input examples/judge/input.json --provider openai --mode live --allow-live --timeout 30 --max-output-tokens 2048
}
finally {
    Remove-Item Env:OPENAI_API_KEY -ErrorAction SilentlyContinue
}
```

Để chạy retrieval thật trước Judge, dùng `demo --context examples/judge/context.json --artifacts-dir THU_MUC_MOI`
với cùng flags provider/mode/allow-live; không dùng BasicRAG.answer() vì hàm baseline đó đã generation.
Model-specific cap thấp hơn 2048 nếu có phải được cấu hình đúng; model reasoning có thể cần token budget lớn hơn.
Không tự tăng token budget/retry khi API trả incomplete.

## Python API để backend gọi

API chính: `OpenAIProvider.from_environment(...)` -> `JudgeAgent(provider)` -> `evaluate(input, telemetry=...)`.
Factory mặc định allow_live=False, thiếu opt-in/key/model raise GenerationError có code tĩnh để backend phát hiện lỗi startup.
Sau khi agent đã khởi tạo, evaluate trả Judge JSON chuẩn, lỗi generation/validation là status ERROR.

Ví dụ đầy đủ dùng bundle/context có sẵn trong checkout (chỉ chạy khi server đã cấu hình key/model và chủ động bật live):

```python
from ai_core.judge_cli import read_json
from examples.judge.backend_openai import create_server_judge, judge_bundle

# Server startup: lấy key/model từ environment server; không lấy key từ request/frontend.
judge = create_server_judge(allow_live=True, timeout_seconds=30, max_output_tokens=2048)

# Mỗi request: bundle từ EvidenceBuilder; question/issues/date/history từ Query/caller.
bundle = read_json("outputs/judge-demo-pilot/bundle.json")
context = read_json("examples/judge/context.json")
report, telemetry = judge_bundle(
    judge, bundle,
    question=context["question"], query_plan=context["query_plan"],
    as_of_date=context["as_of_date"],
    search_rounds_completed=context["search_rounds_completed"],
    query_history=context["query_history"],
    query_text_history=context.get("query_text_history"),
)
# Trả report cho backend/web. Telemetry là dữ liệu riêng phía server.
```

`judge_bundle()` gọi adapter hiện có, không mutate bundle; adapter lỗi thì trả ERROR và telemetry stage=adapter, provider_calls=0.
Mỗi request tạo telemetry dict riêng. Có thể tái sử dụng agent/provider trong service; không chia sẻ telemetry dict giữa các request.
Python API đồng bộ; backend async cần chạy nó trong worker thích hợp. Không có HTTP endpoint mới trong task này.

## Giới hạn, lỗi, telemetry và chi phí

- Mặc định CLI mock/offline. API chỉ được gọi khi `--mode live --allow-live`, hoặc provider được khởi tạo allow_live=True ở server.
- Mỗi evaluate tối đa một POST generation; evidence rỗng/input sai không gọi API. Không có retry hay fallback model/provider.
- Timeout mặc định 30 giây, cấu hình (0,120]; đây là timeout socket/I/O, không phải hard deadline toàn HTTP request.
  Backend/Orchestrator cần deadline ngoài nếu yêu cầu giới hạn tuyệt đối theo wall-clock.
- Giữ giới hạn messages của Judge (100.000 byte mặc định), payload API 120.000 byte, response tối đa 1 MiB,
  output text 24.000 ký tự mặc định. Vượt thì lỗi, không cắt evidence/output.
- Output cap mặc định 2048, client cho phép 1..32768 token; phải nằm trong khả năng model. max_output_tokens bao gồm cả reasoning tokens.
- Provider không log raw prompt/output, upstream error message/body, exception chứa key hoặc environment.
  Không đọc .env/cookie/credential store; endpoint cố định, không dùng OPENAI_BASE_URL, không theo redirect hoặc kế thừa proxy environment.

| Nhóm | error_code tiêu biểu |
| --- | --- |
| Config | OPENAI_LIVE_OPT_IN_REQUIRED, OPENAI_API_KEY_REQUIRED, OPENAI_MODEL_REQUIRED, OPENAI_CONFIGURATION_ERROR |
| Auth/quyền/model | OPENAI_AUTHENTICATION_ERROR (401), OPENAI_PERMISSION_ERROR (403), OPENAI_MODEL_UNAVAILABLE (404) |
| Rate/quota | OPENAI_RATE_LIMIT hoặc OPENAI_QUOTA_EXCEEDED theo error code của API; không retry |
| Network | OPENAI_NETWORK_ERROR, PROVIDER_TIMEOUT |
| Capability/context | OPENAI_UNSUPPORTED_PARAMETER, OPENAI_SCHEMA_OR_MODEL_UNSUPPORTED, OPENAI_CONTEXT_LIMIT, OPENAI_BAD_REQUEST |
| Response | OPENAI_REFUSAL, OPENAI_INCOMPLETE_RESPONSE, OPENAI_EMPTY_RESPONSE, OPENAI_INVALID_RESPONSE, OPENAI_UNEXPECTED_OUTPUT |
| Bound/secret | OPENAI_INPUT_LIMIT, OPENAI_RESPONSE_LIMIT, OPENAI_OUTPUT_LIMIT, OPENAI_SECRET_IN_INPUT/RESPONSE |
| Validator Judge | INVALID_OUTPUT_JSON/SCHEMA/REFERENCE/CONSISTENCY... như trước |

Telemetry riêng chứa requested_model, returned_model, usage, latency_ms, stop_reason/status, http_status, stage/error_code.
returned_model/usage chưa được API cung cấp thì null; không lấy requested_model để giả làm returned_model.
Usage chỉ giữ counts do API trả: input/output/total, cached input và reasoning output khi có; không tự tính token hoặc giá tiền giả.
Chi phí đối chiếu usage thực tế với [bảng giá API](https://developers.openai.com/api/docs/pricing) của đúng model tại thời điểm gọi;
reasoning tokens đã nằm trong output_tokens nên không cộng trùng. Không coi phí/hạn mức ChatGPT Plus là ngân sách API của backend.
Timeout hoặc response lỗi không có nghĩa chắc chắn không phát sinh usage; đối chiếu dashboard API khi cần.

## Kết quả kiểm thử và giới hạn thực tế

**69/69 test Python đạt; 21/21 test Node regression đạt; không failure/error/skipped.**

Log thực: `examples/judge/openai_python_test_run.txt`, `examples/judge/openai_node_regression_run.txt`;
số liệu: `examples/judge/openai_test_result.json`. Riêng provider mới có 19 test methods với nhiều subcases.


Test bao phủ thiếu cấu hình/live gate, payload schema Judge, output hợp lệ/sai ID/status/JSON/schema;
timeout/auth/permission/model/rate/quota/network; refusal/incomplete/empty; một request và không retry/fallback;
empty evidence zero request; secrets không ra telemetry/error; backend adapter example; fresh interpreter PATH rỗng
không import ChatGPT provider, không gọi Node/OAuth/DPAPI. Regression cũ bao gồm BasicRAG, BM25 pilot và Node OAuth/streaming.

Tất cả API HTTP/model trong test đều mock. **Chưa chạy OpenAI API live, chưa xác minh quyền/model/key thực tế hoặc legal accuracy.**
Chưa triển khai LangGraph/stopping loop, HTTP server, async provider, distributed quotas hoặc production load test.
Orchestrator sau này chịu budget/retry/stop, giữ gaps khi hết lượt; provider mới không thay nhãn thiếu thành đủ.

## Tài liệu OpenAI chính thức đã đối chiếu

Đối chiếu ngày 01/11/2026:

- [Responses API create](https://developers.openai.com/api/reference/python/resources/responses/methods/create): endpoint, input/instructions, max_output_tokens, text.format, usage/status.
- [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs): strict JSON Schema, required fields, additionalProperties=false, refusal/incomplete và các hạn chế schema.
- [Python SDK](https://developers.openai.com/api/reference/python): đã kiểm tra retry/timeout; không chọn SDK trong implementation này để giữ stdlib convention và tránh dependency mới.
- [API models](https://developers.openai.com/api/docs/models), [API pricing](https://developers.openai.com/api/docs/pricing): lựa chọn API model ID/capability và đối chiếu usage.

## Vị trí hàm trong code cuối

- `ai_core/openai_provider.py:23` - `openai_output_schema`.
- `ai_core/openai_provider.py:42` - `_usage`.
- `ai_core/openai_provider.py:66` - `OpenAIProvider`.
  - `ai_core/openai_provider.py:94` - `OpenAIProvider.from_environment`.
  - `ai_core/openai_provider.py:102` - `OpenAIProvider.generate`.
- `ai_core/judge_cli.py:17` - `read_json`.
- `ai_core/judge_cli.py:32` - `write_json`.
- `ai_core/judge_cli.py:38` - `configured_provider`.
- `ai_core/judge_cli.py:69` - `_context_payload`.
- `ai_core/judge_cli.py:78` - `main`.
- `examples/judge/backend_openai.py:14` - `create_server_judge`.
- `examples/judge/backend_openai.py:21` - `judge_bundle`.

### Phân biệt HTTP 429

Provider nhận diện billing/quota từ error.code=insufficient_quota/credit_balance_exhausted hoặc error.type=insufficient_quota.
Các trường hợp này trả OPENAI_QUOTA_EXCEEDED; 429 khác trả OPENAI_RATE_LIMIT. Không log upstream error message hoặc tự retry.
Không thể suy ra nguyên nhân của lần lỗi cũ chỉ từ OPENAI_RATE_LIMIT đã được lưu; cần kiểm tra Billing/Limits hoặc chạy lại có chủ đích.
