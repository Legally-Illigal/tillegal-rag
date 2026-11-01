# Judge qua Google Gemini API

Bổ sung ngày 01/11/2026 trong `Tilligal-judge-agent`, nhánh `feature/judge-agent`. Không push/merge. Đã kiểm tra code Judge/provider/CLI/test, bridge Node và tài liệu hiện có; không tìm thấy AGENTS.md trong checkout và các thư mục cha đã kiểm tra. Giữ các thay đổi có sẵn.

## Luồng và file

```text
Backend: input Judge chuẩn ──────────────────────────────┐
BM25 → EvidenceBuilder → adapt_evidence_bundle ───────────┤
                                                        ↓
JudgeAgent: validate input → prompt v1.0 → GeminiProvider
              → google-genai → Gemini Developer API
              → kiểm tra finish/safety → chuỗi JSON
              → strict_json → validate_output → Judge JSON
                                            ↘ telemetry riêng
```

Judge đánh giá sufficiency trước Summary; không làm Search, reranker hay Verify. Không gọi generation của BasicRAG trước Judge. Schema, checklist, adapter và validator giữ nguyên. Chưa triển khai HTTP server hoặc vòng lặp LangGraph: Orchestrator vẫn chịu trách nhiệm budget/retry/stopping; SUFFICIENT→Summary, INSUFFICIENT→tìm tiếp nếu còn budget và tiến triển, nếu không giữ gaps; ERROR→retry giới hạn hoặc kết thúc lỗi.

| File | Thay đổi/trách nhiệm |
|---|---|
| `ai_core/gemini_provider.py` | Mới: `GeminiProvider.from_environment(...)`, `generate(messages) -> str`, `gemini_output_schema()` |
| `ai_core/judge_cli.py` | Thêm lựa chọn `gemini` cho evaluate/demo, lazy import, mặc định output 4096 token |
| `ai_core/judge_agent.py` | Chỉ thêm `provider_name` vào telemetry; giữ logic và prompt v1.0 |
| `examples/judge/backend_gemini.py` | Mới: khởi tạo server, đánh giá input chuẩn hoặc bundle+context, trả `(result, telemetry)` |
| `requirements-gemini.txt` | SDK tùy chọn `google-genai==2.34.0`, Python >=3.11; không sửa dependency baseline |
| `tests/test_gemini_provider.py` | 18 test offline; có transport HTTP giả lập chạy xuyên qua SDK thật |
| `.gitignore` | Cho phép đưa test Gemini vào handoff giống các test Judge khác |
| `examples/judge/gemini-*-tests.txt`, `gemini-node-regression.txt` | Log test kỹ thuật (không chứa key thật) |
| `docs/judge_agent.md` | Thêm liên kết tới báo cáo này |

Không sửa BasicRAG, retrieval, EvidenceBuilder, parser, index hoặc các provider cũ. Đường Gemini không import bridge ChatGPT, không cần Node, OAuth, DPAPI hoặc tài khoản cá nhân. SDK chỉ import trong `generate`, vì vậy mock và nhánh evidence rỗng không cần cài SDK.

## SDK, schema và giới hạn

Chọn API chính thức `client.models.generate_content`, endpoint cố định `https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent`; SDK hiện hành vẫn tài liệu hóa phương thức này. Quickstart mới dùng Interactions; nhiệm vụ đồng bộ một lần của Judge dùng generateContent để tránh thêm session/storage/agent loop. Không dùng SDK cũ `google-generativeai` hoặc endpoint ChatGPT.

System message→`system_instruction`; user message→một `Content(role="user", parts=[Part(text=...)])`. Giữ nguyên câu hỏi gốc và evidence như dữ liệu, không chuyển instruction trong evidence sang system. Không yêu cầu chain-of-thought; không gửi `temperature`, tham số OpenAI/Anthropic hoặc cấu hình thinking suy từ tên model. Thought parts nếu có không được parse hoặc log.

`response_mime_type="application/json"`, `response_json_schema=gemini_output_schema()`. Schema gửi đi giữ đúng 8 trường Judge, required, enum, null, additionalProperties và giới hạn số phần tử. Bỏ `minLength`, `maxLength`, `pattern`, `uniqueItems` khỏi bản schema gửi provider để dùng tập tính năng portable; **không sửa OUTPUT_SCHEMA gốc**. `strict_json` và validator Python vẫn kiểm tra toàn bộ constraint/ID/status/coverage/issue coverage; không sửa response sai thành hợp lệ. Đây không phải schema draft của BasicRAG.

Giới hạn hiện có: Judge mặc định 100000 byte input kể cả prompt/schema, không cắt evidence; provider chặn thêm ở 120000 byte. Output mặc định 24000 ký tự; token mặc định 4096, cấu hình được qua `--max-output-tokens` hoặc constructor (1..32768). Thinking có thể dùng chung ngân sách output; `MAX_TOKENS` luôn là ERROR kể cả JSON trông hợp lệ. Tăng budget chỉ khi bạn chủ động chọn; không tự gọi tiếp. Model không hỗ trợ schema/tham số sẽ báo lỗi, không tự chuyển chế độ/model.

Timeout mặc định 30 giây, SDK `HttpOptions.timeout=30000` mili giây: timeout HTTP của transport, **không phải deadline cứng cho toàn bộ evaluate**. Backend cần deadline worker/process nếu muốn giới hạn toàn request. `HttpRetryOptions(attempts=1)` tắt retry; tắt cả automatic function calling và `automatic_continuation` (SDK mới có thể tự tiếp tục mặc định). Tối đa một generate request; không list model, refresh token, tools, fallback hoặc bật billing. Evidence rỗng hợp lệ trả INSUFFICIENT từ JudgeAgent với 0 provider call.

## Cài và chạy trên PowerShell

Trong checkout này có Python 3.14 phù hợp index Unicode 16. Không bỏ integrity check nếu dùng runtime khác không tương thích. Python backend mới có thể dùng venv >=3.11 riêng; smoke index cần đúng runtime của manifest.

```powershell
cd 'D:\Trang\TilLegal (Legal AI)\Tilligal-judge-agent'
$py = '.\.tmp\python314\python.exe'

# Mock không cần key, SDK hoặc đăng nhập
& $py -B -m ai_core.judge_cli evaluate --input examples/judge/input.json --provider gemini --mock-response examples/judge/sufficient.json

# Cài trên Python/venv có pip:
python -m pip install -r requirements-gemini.txt
```

Python nhúng `.tmp/python314` của checkout không có pip. SDK đã cài riêng vào `.tmp/python314/site-packages`, thêm dòng `site-packages` trong `python314._pth`; đây là cấu hình local bị gitignore. Nếu cần cài lại đúng runtime này trên máy hiện tại:

```powershell
$installer = "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
& $installer -m pip --python $py install --target '.tmp/python314/site-packages' --only-binary=:all: -r requirements-gemini.txt
# Bảo đảm file .tmp/python314/python314._pth có dòng site-packages.
```

Tạo key tại [Google AI Studio API keys](https://aistudio.google.com/apikey), chọn đúng Google project. Kiểm tra model và quota đang cấp cho project, tier/billing trong AI Studio trước khi chạy. Không dùng tên hiển thị ChatGPT làm model ID. Theo tài liệu đối chiếu, `gemini-2.5-flash` chỉ còn cho người/project đã sử dụng trước đó; không xác nhận được Free Tier phổ quát của model này cho project mới. Vì vậy lệnh yêu cầu nhập model ID bạn đã kiểm tra, không mặc định chọn 2.5. Pricing hiện có Free Tier cho `gemini-3.5-flash-lite`; đây chỉ là ví dụ để kiểm tra trong project, không tự chọn thay model của bạn và chưa kiểm chứng live trong nhiệm vụ.

```powershell
$env:GEMINI_MODEL = Read-Host 'Nhap model ID da kiem tra trong Google AI Studio'
$geminiSecret = Read-Host 'Gemini API key' -AsSecureString
$env:GEMINI_API_KEY = [System.Net.NetworkCredential]::new('', $geminiSecret).Password
$run = "outputs/gemini-$([guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Path $run | Out-Null
try {
  & $py -B -m ai_core.judge_cli evaluate --input examples/judge/input.json --provider gemini --mode live --allow-live --timeout 30 --max-output-tokens 4096 --output "$run/result.json" --telemetry "$run/telemetry.json"
}
finally {
  Remove-Item Env:GEMINI_API_KEY -ErrorAction SilentlyContinue
  Remove-Variable geminiSecret -ErrorAction SilentlyContinue
}
Get-Content "$run/result.json" -Raw -Encoding UTF8
Get-Content "$run/telemetry.json" -Raw -Encoding UTF8
```

Không dán key trực tiếp vào command, frontend, URL, fixture hoặc git. Ctrl+C dừng tiến trình; request đã tới Google vẫn có thể tính usage. Mỗi lệnh live là một request riêng, không phải dịch vụ chạy nền. Thiếu env hoặc chưa opt-in báo lỗi cấu hình; không tìm key từ máy, không fallback. CLI mặc định mock kể cả `--provider gemini`. Cần cả `--mode live --allow-live` để gọi thật; Python API có `allow_live=True` tường minh tương đương.

Demo BM25→EvidenceBuilder→adapter→Judge Gemini, sau khi đã đặt env như trên; artifacts-dir phải chưa tồn tại:

```powershell
$demo = "outputs/gemini-demo-$([guid]::NewGuid().ToString('N'))"
try {
  & $py -B -m ai_core.judge_cli demo --context examples/judge/context.json --provider gemini --mode live --allow-live --max-output-tokens 4096 --artifacts-dir $demo
}
finally { Remove-Item Env:GEMINI_API_KEY -ErrorAction SilentlyContinue }
```

Đặt env lại nếu đã chạy khối finally trước đó. Bỏ `--mode live --allow-live` để smoke retrieval offline; mặc định trả MOCK_NOT_EVALUATED. `bundle.json`/`input.json` là artifacts bạn chủ động yêu cầu chứa evidence, không phải telemetry; không đưa thư mục run có dữ liệu nhạy cảm vào frontend/log công khai.

## Python cho backend

```python
from examples.judge.backend_gemini import create_server_judge, judge_input, judge_bundle

# Server startup: GEMINI_API_KEY, GEMINI_MODEL từ secret/config phía server.
agent = create_server_judge(allow_live=True, timeout_seconds=30, max_output_tokens=4096)

# Trong handler/service, value là input Judge chuẩn từ backend.
result, telemetry = judge_input(agent, value)
# Trả result (dict JSON) cho caller; lưu telemetry riêng nếu cần.

# Nếu caller đang có bundle EvidenceBuilder:
result, telemetry = judge_bundle(
    agent, bundle, question=original_question, query_plan=query_plan,
    as_of_date=as_of_date, search_rounds_completed=rounds,
    query_history=query_ids, query_text_history=query_texts,
)
```

`create_server_judge` không gọi mạng; cấu hình sai ném GenerationError với mã tĩnh để startup thất bại rõ. `judge_input` gọi validator của JudgeAgent; `judge_bundle` xử lý lỗi adapter thành ERROR. Question/issues/date/history do caller cung cấp; không tự tạo issues, không tích lũy evidence trong provider. Constructor trực tiếp tương đương: `JudgeAgent(GeminiProvider.from_environment(allow_live=True))`. API đồng bộ; backend async nên chạy trong worker. Không nhận API key hoặc model tùy ý từ request frontend.

## Lỗi và telemetry

| Trường hợp | error_code |
|---|---|
| Thiếu opt-in/key/model; cấu hình sai | GEMINI_LIVE_OPT_IN_REQUIRED / GEMINI_API_KEY_REQUIRED / GEMINI_MODEL_REQUIRED / GEMINI_CONFIGURATION_ERROR |
| Thiếu SDK | GEMINI_DEPENDENCY_MISSING |
| 401 hoặc reason API_KEY_INVALID/EXPIRED | GEMINI_AUTHENTICATION_ERROR |
| 403; 404 | GEMINI_PERMISSION_DENIED; GEMINI_MODEL_UNAVAILABLE |
| 429 (rate hoặc quota) | GEMINI_QUOTA_OR_RATE_LIMIT |
| HTTP/transport timeout; network | GEMINI_TIMEOUT; GEMINI_NETWORK_ERROR |
| 400 khác; 5xx; lỗi SDK | GEMINI_BAD_REQUEST; GEMINI_SERVER_ERROR; GEMINI_SDK_ERROR |
| Safety/recitation/prompt block; rỗng | GEMINI_SAFETY_BLOCK; GEMINI_EMPTY_RESPONSE |
| MAX_TOKENS/CONTINUATION/finish khác STOP | GEMINI_INCOMPLETE_RESPONSE |
| Content không phù hợp; vượt giới hạn | GEMINI_INVALID_RESPONSE; GEMINI_INPUT_LIMIT / GEMINI_OUTPUT_LIMIT |
| JSON/schema/ID/quan hệ sai | Các mã INVALID_OUTPUT_* từ validator hiện có |

ERROR giữ shape chuẩn với các danh sách đánh giá rỗng và error_code khác null. Lỗi quota không phải thiếu evidence. Không tự phân biệt rate/quota nếu Google chỉ trả HTTP429; kiểm tra AI Studio để biết hạn mức cụ thể, không retry mù.

Telemetry: `provider_name="gemini"`, `model`, `prompt_version`, `provider_mode`, `provider_calls`, `stage`, `latency_ms`, `error_code`; bên trong `provider`: `requested_model`, `returned_model` từ model_version nếu có, `usage` gồm các token count có thực, `stop_reason` thuộc allowlist, `http_status` khi SDK error có code, latency provider. Không suy returned_model bằng requested_model; thiếu thì null. provider_calls là số lần Judge gọi generate, có thể là 1 dù lỗi xảy ra trước HTTP; test transport kiểm tra số HTTP thực. Lỗi cấu hình trước khi tạo agent hiện không có telemetry provider; CLI trả ERROR rõ ràng. Không lưu exception message, headers, raw prompt/output, evidence hay key. Không tự tính USD từ số token vì giá/tier/thinking thay đổi; đối chiếu usage thực với trang pricing và project billing.

Free Tier do project/quyền lợi Google quyết định; model có Free Tier không bảo đảm mọi lượt miễn phí. Rate limit áp dụng theo project, không theo key; kiểm tra RPM/TPM/RPD thực tế trong AI Studio. Code không tự bật billing. Với Unpaid Services, Google có thể dùng input/output để cải thiện sản phẩm và người đánh giá có thể đọc/xử lý; Terms yêu cầu không gửi dữ liệu nhạy cảm, bí mật, cá nhân. Dùng fixture tổng hợp hoặc nguồn công khai thích hợp để thử. Paid Services có chính sách khác và vẫn có lưu dữ liệu giới hạn cho an toàn; Terms có ngoại lệ theo khu vực/tài khoản, nhóm cần đối chiếu đúng project. Không quảng bá Free Tier là chế độ bảo mật cho hồ sơ khách hàng.

## Kiểm thử và giới hạn đã biết

Đã chạy bằng Python3.14.2 + google-genai2.34.0: **87 test Python pass, 0 skip**, gồm **18 test Gemini mới**; Node hồi quy **21 pass**. Log lượt sửa tương thích mới: `examples/judge/gemini-python-compatibility-tests.txt`; log trước đó: `examples/judge/gemini-python-tests.txt`, `examples/judge/gemini-node-regression.txt`.

```powershell
& $py -B -m unittest tests.test_gemini_provider -v
& $py -B -m unittest discover -s tests -v
node --test tests/chatgpt.test.mjs  # Chỉ hồi quy provider cũ; Gemini không cần Node.
```

Test kiểm tra env/opt-in/lazy dependency, SDK payload/schema mapping, validator JSON/ID/status/coverage, timeout/auth/403/404/429/network, safety/empty/token exhaustion/CONTINUATION, một request kể cả429, không fallback, evidence rỗng, unknown metadata, không lộ key và backend BM25→adapter. Test injection chỉ kiểm tra nó ở user data, không chứng minh model chống injection. SDK tests sẽ skip có thông báo nếu chưa cài dependency; lượt báo cáo trên đã cài và không skip.

Đã chạy CLI demo offline với index thật tại `outputs/judge-gemini-offline-20261101/`: bundle/input/result/telemetry được tạo, có 19 evidence với 2687 ký tự nội dung, input prompt tổng cộng 62270 byte; kết quả `ERROR/MOCK_NOT_EVALUATED`, `provider_calls=1` là gọi FixtureJudgeProvider, không phải HTTP. Điều này xác nhận đường retrieval+adapter, chưa kết luận sufficiency. Test SDK dùng httpx.MockTransport và key/model giả; không có generation live hoặc xác minh compatibility/model access/quota thực. Chưa đánh giá chất lượng ngữ nghĩa/pháp lý; cần human gold và live eval được nhóm chủ động cho phép. Giữ baseline và dữ liệu index nguyên trạng.

## Nguồn chính thức đã đối chiếu

- [Quickstart hiện chuyển tới Getting started](https://ai.google.dev/gemini-api/docs/quickstart): SDK và cấu hình server API key.
- [Structured output](https://ai.google.dev/gemini-api/docs/structured-output): tập JSON Schema hỗ trợ, giới hạn decoder, vẫn cần kiểm tra ở client.
- [SDK Python chính thức](https://googleapis.github.io/python-genai/): generate_content, response_json_schema, client lifecycle/HTTP options. Đối chiếu thêm source SDK2.34.0 đã cài cho retry, timeout, automatic_continuation.
- [Pricing](https://ai.google.dev/gemini-api/docs/pricing): Free/Paid, model và thinking token.
- [Rate limits](https://ai.google.dev/gemini-api/docs/rate-limits): quota theo project và AI Studio.
- [Deprecations](https://ai.google.dev/gemini-api/docs/deprecations): giới hạn truy cập dòng2.5.
- [Additional Terms](https://ai.google.dev/gemini-api/terms): Unpaid/Paid Services và sử dụng dữ liệu.

Thông tin model/quota/giá có thể thay đổi; cấu hình model luôn tường minh, không tự chuyển khi lỗi.

## Sửa tương thích sau khi nhận lỗi live (01/11/2026)

Rà lại theo [Google troubleshooting](https://ai.google.dev/gemini-api/docs/troubleshooting), bảng model parameters ghi candidate count không được hỗ trợ trên Gemini 3 và 4. Provider trước đây gửi candidate_count=1 cho mọi model; đây là lỗi tương thích đã sửa bằng cách bỏ tham số này cho tất cả model, vẫn kiểm tra response có đúng một candidate. Không suy tham số từ tên model, không sửa schema/checklist/adapter.

Test HTTP mock nay từ chối bất kỳ payload nào có candidateCount; thêm test rõ cho ID 2.5 Flash và 3.8 Flash, kiểm tra SDK không gửi tham số đó và chỉ có một request. Thêm HTTP503 vào bảng test lỗi. Toàn bộ 87 Python test pass, không skip; không gọi Gemini live. Các test và tên model trong mock không chứng minh quyền truy cập hoặc live compatibility.

Phân biệt kết luận: thông báo Google do người dùng cung cấp cho 2.5 Flash xác nhận model không còn cho người dùng mới; sửa code không mở quyền này. HTTP503 và timeout ở 3.8 chưa đủ chứng cứ xác định nguyên nhân; tham số vừa sửa chưa được chứng minh là nguồn gây các lỗi đó. Không cam kết rằng sau bản sửa live chắc chắn thành công. Khi thử lại, có thể chủ động dùng --timeout 90 để tăng HTTP timeout; vẫn không retry/fallback. Không tự tăng timeout mặc định hoặc bật billing. Node 21 test là kết quả hồi quy của lượt trước; không chạy lại Node trong lượt sửa chỉ thuộc Gemini này.
## Chẩn đoán lỗi kỹ thuật sau bản sửa tương thích

Provider trước đây gom HTTP500/503 vào GEMINI_SERVER_ERROR nên khó xác định vấn đề khi gọi CLI. Nay HTTP503 trả GEMINI_SERVICE_UNAVAILABLE; HTTP500 vẫn GEMINI_SERVER_ERROR. Telemetry riêng bổ sung api_status (chỉ các mã Google thuộc allowlist), error_reason (chỉ nhãn cố định MODEL_ACCESS_RESTRICTED/MODEL_OVERLOADED/MODEL_HIGH_DEMAND/TEMPORARILY_UNAVAILABLE khi nhận ra tín hiệu trong message), timeout_kind (connect/read/write/pool/transport/upstream_http). Không lưu message gốc, headers, raw response, prompt/evidence hoặc key; không nhận ra thì null. Nhãn là phân loại thông báo, không xác nhận toàn bộ nguyên nhân gốc. JudgeAgent chỉ mở rộng allowlist telemetry, không sửa output contract/checklist/schema. Mỗi evaluate vẫn tối đa một request, không tự retry hoặc fallback.

Nguồn đối chiếu: https://ai.google.dev/gemini-api/docs/interactions xác nhận generateContent legacy vẫn được hỗ trợ đầy đủ; vì vậy chưa có căn cứ coi đổi sang Interactions là cách sửa lỗi 503. https://ai.google.dev/gemini-api/docs/troubleshooting hướng dẫn retry có giới hạn cho lỗi tạm thời; Orchestrator/caller vẫn quyết định retry trong dự án này.

Lấy telemetry an toàn sau khi người dùng chủ động chạy live (mỗi lần một generation request):

```powershell
$t = "outputs/gemini-diag-$([guid]::NewGuid().ToString('N')).json"
& $py -B -m ai_core.judge_cli evaluate --input examples/judge/input.json --provider gemini --model gemini-3.8-flash --mode live --allow-live --timeout 90 --max-output-tokens 4096 --telemetry $t
if (Test-Path $t) { Get-Content $t -Raw -Encoding UTF8 }
```

Test hồi quy mới kiểm tra không đưa message/key vào telemetry, 503 riêng, timeout connect/read và HTTP504 có phân loại khác nhau, không thêm request. Log lượt này: examples/judge/gemini-python-diagnostics-tests.txt. Chưa gọi live, chưa xác minh nguyên nhân 503/timeout thực tế của project người dùng.
Kết quả lượt chẩn đoán: 89 test Python pass, 0 skip (20 test Gemini). Chỉ kiểm thử offline SDK/HTTP mock; không chạy lại Node hoặc gọi Gemini live trong lượt này.
