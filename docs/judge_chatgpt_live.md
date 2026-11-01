# Chạy Judge bằng tài khoản ChatGPT

Cập nhật 01/11/2026. Provider mới nằm cùng checkout `Tilligal-judge-agent`, nhánh `feature/judge-agent`.
BasicRAG, retrieval, EvidenceBuilder và output contract Judge vẫn giữ nguyên. Provider Anthropic vẫn có thể dùng như trước.

## 1. Chạy từng bước, không cần API key

Mở PowerShell rồi copy dòng sau:

```powershell
cd 'D:\Trang\TilLegal (Legal AI)\Tilligal-judge-agent'
```

Đăng nhập:

```powershell
.\judge-chatgpt.cmd Login
```

Trình duyệt mở trang đăng nhập **OpenAI**. Đăng nhập tài khoản ChatGPT Plus của bạn, chọn đúng tài khoản/workspace,
đọc và cho phép **Tillegal Judge** dùng gói ChatGPT. Không nhập mật khẩu hay token vào PowerShell/chat này.
Quay lại PowerShell sau khi trình duyệt nhận callback. Thành công khi output có `status: connected`, `sharing: true`.
Callback trang web chỉ xác nhận đã nhận code; kết quả trong PowerShell mới xác nhận identity và scope hợp lệ.
Nhấn Ctrl+C nếu muốn hủy. Login mặc định chờ tối đa 300 giây.

Xem các model được tài khoản cấp quyền:

```powershell
.\judge-chatgpt.cmd Models
```

Chạy Judge live trên data BM25 của repo:

```powershell
.\judge-chatgpt.cmd Live
```

Lệnh tự lấy model đầu tiên trong danh sách được tài khoản cho phép. Không hard-code model hoặc đổi nhãn mock thành live.
Nếu muốn chọn model khác, dùng `-Model` với **slug đúng do lệnh Models trả về**.
Phiên bản này không hỏi model ID bằng Read-Host nên không có bước nhập key Anthropic.

Live gửi question/issues/evidence lên OpenAI và tiêu thụ hạn mức ChatGPT của tài khoản.
Việc Plus hoặc account cụ thể có được cấp quyền/model nào phải xác nhận sau đăng nhập và gọi thành công,
không được suy ra chỉ từ việc bạn đã trả phí Plus. Đây là luồng SIWC preview theo tài liệu chính thức.

## 2. Xem output thật và biết model đã chạy

Luồng Live: corpus BM25 -> retrieval -> EvidenceBuilder -> adapter -> ChatGPTProvider -> output validation -> Judge result.
Không có generation trước Judge. Không dùng `--mock-response` trong lệnh Live.

Mỗi lần chạy tạo thư mục mới `outputs/judge-chatgpt-live-<ID>/`, chứa:

| File | Nội dung |
| --- | --- |
| `bundle.json` | Bundle thật từ EvidenceBuilder, giữ ID/metadata/limitations |
| `input.json` | Question/issues/evidence/context thực tế truyền cho Judge |
| `result.json` | Output chuẩn SUFFICIENT/INSUFFICIENT/ERROR |
| `telemetry.json` | provider_mode, model được yêu cầu, returned_model thực, prompt version, latency và lỗi |

Script in kết quả cùng đường dẫn thư mục và telemetry. Với inference hoàn tất, telemetry có `provider_mode: live`
và `provider.stop_reason: response.completed`; returned_model ghi model từ response của OpenAI.
Nếu sau đó output không qua validation thì result vẫn ERROR, ví dụ INVALID_OUTPUT_SCHEMA/REFERENCE/CONSISTENCY.
Không coi HTTP 200 hoặc stream có một phần text là thành công. Không tự sửa JSON/ID/coverage để hợp thức hóa.

Để xem lại lần chạy gần nhất:

```powershell
$latest = Get-ChildItem -Directory 'outputs/judge-chatgpt-live-*' | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($latest) {
    Get-Content (Join-Path $latest.FullName 'result.json') -Raw -Encoding UTF8
    Get-Content (Join-Path $latest.FullName 'telemetry.json') -Raw -Encoding UTF8
}
```

Để tự đánh giá chất lượng, đối chiếu issue_checks với nội dung E-ID trong input.json, xem gap có liên quan và query
bổ sung có giúp tìm phần thiếu không. SUFFICIENT vẫn cần human gold để đánh giá độ chính xác; Judge không thay Verify.

## 3. Test offline, xem phiên và đăng xuất

```powershell
.\judge-chatgpt.cmd Tests
.\judge-chatgpt.cmd Status
.\judge-chatgpt.cmd Logout
```

Tests chạy cả unittest Python và Node built-in tests. Không gọi inference, không đăng nhập tài khoản thật.
Status chỉ hiển thị trạng thái/profile/email đã được xác minh, không có access/refresh token.
Logout thử revoke phiên trên OpenAI, sau đó xóa token local và giữ client registration/host ID để đăng nhập lại.
Nếu remote_revocation_confirmed=false, phiên local đã bị xóa nhưng cần vào ChatGPT Settings để disconnect app khi có mạng.

Thêm tài khoản khác hoặc chọn profile đã lưu bằng CLI:

```powershell
$py = '.\.tmp\python314\python.exe'
& $py -B -m ai_core.judge_cli chatgpt-login --new-account
& $py -B -m ai_core.judge_cli chatgpt-status
# Dùng --profile với profile_id do Status trả về để chọn lại tài khoản đã lưu.
```

## 4. CLI và Python API chi tiết

Wrapper `.cmd` chỉ gọi PowerShell với ExecutionPolicy Bypass cho **process đó**, không thay policy toàn máy.
Script tự tìm Python đã chuẩn bị trong `.tmp/python314`; không phụ thuộc biến `$py` từ cửa sổ khác.
Node.js 22+ cần có trên PATH. Môi trường đã kiểm tra dùng Node 24.22.1, Python 3.14.2, Unicode 16.
Không cài thêm package Python/npm cho provider này; chỉ dùng thư viện chuẩn Python, Node và Windows DPAPI.

Nếu dùng lệnh Python trực tiếp:

```powershell
$py = '.\.tmp\python314\python.exe'
& $py -B -m ai_core.judge_cli chatgpt-login
& $py -B -m ai_core.judge_cli chatgpt-models
$run = "outputs/judge-live-$([guid]::NewGuid().ToString('N'))"
& $py -B -m ai_core.judge_cli demo --context examples/judge/context.json --artifacts-dir $run --mode live --provider chatgpt --allow-live --timeout 120
```

Hoặc đánh giá một input Judge chuẩn đã có bằng `evaluate --input PATH --mode live --provider chatgpt --allow-live`.
Chọn Anthropic như cũ bằng `--provider anthropic` hoặc bỏ provider (mặc định không thay đổi).

Python API sau khi đăng nhập bằng CLI:

```python
from ai_core.chatgpt_provider import ChatGPTProvider
from ai_core.judge_agent import JudgeAgent
from ai_core.judge_cli import read_json

telemetry = {}
agent = JudgeAgent(ChatGPTProvider(allow_live=True, timeout_seconds=120))
result = agent.evaluate(read_json("examples/judge/input.json"), telemetry=telemetry)
```

`model="chatgpt-account-default"` chọn model đầu tiên từ catalog tài khoản. Có thể truyền slug cụ thể.
`ChatGPTProvider` không nhận API key. Nếu cần gọi OpenAI bằng API key/tính phí Platform thì đó là một provider khác, chưa triển khai ở đây.

## 5. Implementation thực tế

| File / hàm | Trách nhiệm |
| --- | --- |
| `ai_core/chatgpt_provider.py:ChatGPTProvider.generate` | Protocol generate(messages); lấy text hoàn tất qua Node, trả GenerationText với metadata an toàn |
| `ai_core/chatgpt_provider.py:chatgpt_command` | JSON stdin/stdout với Node, timeout process, không truyền token qua argv |
| `ai_core/judge_cli.py:configured_provider` | Chọn ChatGPT/Anthropic, bắt live opt-in; mock vẫn độc lập |
| `tools/chatgpt/bridge.mjs` | Action login/status/models/select/logout/generate; JSON stdout, lỗi chỉ có code |
| `tools/chatgpt/auth.mjs` | OAuth PKCE/state/nonce, callback loopback, dynamic registration, ID token, refresh/checkpoint, account selection, revoke |
| `tools/chatgpt/store.mjs` | Lưu profile riêng, write atomic, process lock, không reset file hỏng |
| `tools/chatgpt/dpapi.ps1` | Windows DPAPI CurrentUser; dữ liệu đi qua pipe, không qua command-line hoặc plaintext file |
| `tools/chatgpt/common.mjs` | HTTP JSON có bound/timeout, no redirects, mã lỗi; subprocess DPAPI |
| `tools/chatgpt/transport.mjs` | Catalog tài khoản, Responses streaming, output limit, chỉ chấp nhận response.completed |
| `scripts/check_judge_chatgpt.ps1`, `judge-chatgpt.cmd` | Lệnh chạy đơn giản và kiểm thử từ repo root |
| `tests/test_chatgpt.py`, `tests/chatgpt.test.mjs` | Python/provider/CLI và OAuth/HTTP/SSE/DPAPI tests với dữ liệu giả |

Code tự triển khai theo giao thức công khai bằng thư viện chuẩn. Không đóng gói hoặc phụ thuộc source DevKit đã tải để tham khảo.

## 6. Authentication và bảo vệ phiên

- Lần đầu dùng dynamic_agent_client, app name Tillegal Judge, host UUID riêng và callback HTTP trên **127.0.0.1**, `/auth/callback`.
- Mỗi attempt có PKCE S256, state và nonce mới. Sai state/duplicate callback/client ID khác bị từ chối.
- Client ID đã được cấp được lưu trước code exchange; lần sau tái sử dụng thay vì tạo lại registration.
- Xác minh chữ ký ID token bằng JWKS lấy từ issuer chính thức; algorithm allowlist RS256/ES256; kiểm tra issuer/audience/expiry/nonce/subject.
- Phải có scope chatgpt.tokens.use.direct trong token response trước inference. Identity sign-in riêng không đủ.
- Không đổi active account trước khi identity/scopes hợp lệ. Profile/client/token không trộn giữa các tài khoản.
- Tokens, identity và host state lưu tại `%LOCALAPPDATA%\Tillegal\Judge\ChatGPT\session.dpapi.json`, mã hóa bằng DPAPI CurrentUser.
- Token không đi vào Python, argv, telemetry, Git hay browser storage. Không đọc cookie/trích phiên ChatGPT hoặc credential của Codex.
- Refresh chạy trong khóa liên tiến trình, dùng issued client ID; replacement được checkpoint mã hóa trước JWKS validation để không mất rotating token khi mạng lỗi.
- File lỗi/không giải mã được được giữ nguyên và báo lỗi. Không âm thầm reset hoặc ghi đè bằng state trắng.

Implementation chỉ hỗ trợ Windows cho credential storage; chưa có keychain Linux/macOS.
DPAPI bảo vệ secret at rest theo tài khoản Windows, không bảo vệ trước mã độc đang chạy với cùng tài khoản đó.

## 7. Request, giới hạn và lỗi

Endpoint inference: `https://api.openai.com/v1/responses`, OAuth bearer từ profile đã được chọn.
System policy Judge đi vào `instructions`; question/evidence là user input. Request có store=false, stream=true;
không dùng backend-api ChatGPT, cookie hoặc API key Anthropic/OpenAI. Không gửi tools để model tự fetch/execute instruction trong evidence.

SIWC preview không hỗ trợ max_output_tokens. Vì vậy `--max-output-tokens` bị từ chối khi provider=chatgpt với
CHATGPT_TOKEN_LIMIT_UNSUPPORTED; không âm thầm bỏ tham số. Giới hạn thay thế: input messages như Judge hiện có,
output 24.000 code points mặc định và deadline toàn luồng HTTP/model 120 giây trong wrapper Live.
Response stream tổng tối đa 1 MiB. Vượt giới hạn thì abort/error; không trả bản cắt rồi kết luận đủ.
Khác Anthropic structured output: provider ChatGPT này dùng schema trong prompt và validation Python;
chưa bật constrained structured decoding trên route SIWC preview. Không tuyên bố server bảo đảm JSON schema.

| error_code | Cách xử lý |
| --- | --- |
| CHATGPT_NODE_REQUIRED / NODE_22_REQUIRED | Cài Node.js 22+ và mở lại PowerShell |
| CHATGPT_SIGN_IN_REQUIRED | Chạy Login, chọn tài khoản Plus đủ điều kiện |
| CHATGPT_SHARING_NOT_ENABLED / PERMISSION_DENIED | Kiểm tra quyền dùng gói ChatGPT, account/workspace và policy; đăng nhập/cấp quyền lại |
| CHATGPT_MODEL_UNAVAILABLE | Chạy Models và chọn slug đang có, hoặc bỏ -Model để chọn mặc định của catalog |
| CHATGPT_USAGE_LIMIT | Kiểm tra ChatGPT Settings → Usage; hạn mức tài khoản không do Judge nâng lên |
| PROVIDER_TIMEOUT / CHATGPT_LOGIN_TIMEOUT | Kiểm tra mạng, chạy lại; không coi đây là thiếu evidence |
| CHATGPT_BAD_REQUEST trong login/refresh | Phiên/grant/preview có thể không được chấp nhận; thử Login lại và cung cấp error_code nếu cần chẩn đoán |
| CHATGPT_INVALID_ID_TOKEN / ACCOUNT_MISMATCH | Dừng sử dụng response; đăng nhập đúng account; không bỏ validation để chạy qua |
| CHATGPT_SECURE_STORAGE_ERROR / STORAGE_INVALID | Kiểm tra tài khoản Windows và quyền truy cập; không chia sẻ hoặc xóa bừa file credential |
| CHATGPT_STORAGE_BUSY | Một tiến trình khác đang dùng phiên; đợi nó hoàn tất hoặc hủy login cũ |
| CHATGPT_STORAGE_LOCK_STALE | Tiến trình cũ đã chết nhưng còn khóa; xem hướng dẫn phục hồi bên dưới |
| CHATGPT_STREAM_INTERRUPTED / INCOMPLETE_RESPONSE / REFUSED | Không có kết quả hoàn tất; input/giới hạn/mạng cần kiểm tra |
| INVALID_OUTPUT_* | Model có thể đã chạy nhưng output không đạt contract; xem telemetry; không đổi thành SUFFICIENT |

Khi có stale lock, đóng tiến trình Login/Live của **Tillegal** còn chạy trước. Sau khi chắc chắn không còn tiến trình đó,
chỉ xóa file khóa, giữ nguyên session.dpapi.json:

```powershell
Remove-Item -LiteralPath "$env:LOCALAPPDATA\Tillegal\Judge\ChatGPT\session.lock"
```

Không có tự động xóa khóa theo tuổi vì có thể làm hai process cùng refresh token. Khi Node nhận tín hiệu hủy,
luồng request/login được hủy rồi giải phóng khóa trong finally, không mở khóa giữa một lần ghi/rotation.
Parent Python hoặc hệ điều hành kill process cưỡng bức, Ctrl+C trong lúc helper đang bận, hoặc mất điện vẫn có thể để lại khóa.
Không tự retry inference; caller/Orchestrator quyết định retry hữu hạn.

## 8. Kiểm thử và mức độ đã xác minh

**50/50 test Python đạt, 21/21 test Node đạt; không skipped.**

Log: `examples/judge/test_run.txt`, `examples/judge/chatgpt_node_test_run.txt`; số liệu: `examples/judge/chatgpt_test_result.json`.


Đã chạy thật: PowerShell helper Tests, Python -> Node process guard, loopback callback với OAuth server giả,
chữ ký RSA thật trên ID token giả, DPAPI roundtrip thật trên Windows với dữ liệu giả,
encrypted persistence, fake model catalog/stream, và regression corpus BM25 hiện có.

Đã probe endpoint công khai `https://auth.openai.com/.well-known/openid-configuration`: HTTP 200,
issuer đúng, JWKS URI và revocation endpoint đúng domain OpenAI, signing alg công bố RS256.
Đây không phải login hoặc inference. Chưa gửi question/evidence lên model thật trong quá trình triển khai.

**Chưa xác minh live trên tài khoản Plus của bạn**: cần bạn thực hiện Login và Live theo mục 1.
Tests fake không chứng minh quyền account/model thực tế, endpoint preview hoạt động E2E với account đó,
độ chính xác sufficiency hoặc khả năng kháng prompt injection của LLM.
Kết quả cần đánh giá bằng bộ human gold trước khi kết luận Judge chính xác về pháp lý.

## 9. Nguồn giao thức chính thức

- [Registration and sign-in](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
- [Accounts and sessions](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions)
- [Models and inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)
- [Preview limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)

Tài liệu được đối chiếu ngày 01/11/2026; các hạn chế preview có thể thay đổi. Không có push/merge trong nhiệm vụ này.

## Vị trí định nghĩa code

- `ai_core/chatgpt_provider.py:20` - `chatgpt_command`.
- `ai_core/chatgpt_provider.py:57` - `ChatGPTProvider`.
- `ai_core/judge_cli.py:17` - `read_json`.
- `ai_core/judge_cli.py:32` - `write_json`.
- `ai_core/judge_cli.py:38` - `configured_provider`.
- `ai_core/judge_cli.py:63` - `_context_payload`.
- `ai_core/judge_cli.py:72` - `main`.
- `tools/chatgpt/auth.mjs:6` - `export const ISSUER = 'https://auth.openai.com';`.
- `tools/chatgpt/auth.mjs:7` - `export const RESOURCE = 'https://api.openai.com/v1';`.
- `tools/chatgpt/auth.mjs:8` - `export const TOKEN_URL = ISSUER + '/api/accounts/oauth/token';`.
- `tools/chatgpt/auth.mjs:9` - `export const SHARING_SCOPE = 'chatgpt.tokens.use.direct';`.
- `tools/chatgpt/auth.mjs:14` - `export function createAttempt()`.
- `tools/chatgpt/auth.mjs:19` - `export function authorizeURL(attempt, redirect, hostId, profile)`.
- `tools/chatgpt/auth.mjs:30` - `export function callbackResult(url, attempt, oldClientId)`.
- `tools/chatgpt/auth.mjs:46` - `export function validateIdToken(token, jwks,`.
- `tools/chatgpt/auth.mjs:74` - `export async function discovery(fetchImpl = fetch, signal)`.
- `tools/chatgpt/auth.mjs:84` - `export async function verifyIdentity(idToken, settings, fetchImpl = fetch, signal)`.
- `tools/chatgpt/auth.mjs:99` - `export function safeProfile(profile)`.
- `tools/chatgpt/auth.mjs:103` - `export function openSystemBrowser(url)`.
- `tools/chatgpt/auth.mjs:111` - `export async function callbackListener(attempt, oldClientId, signal)`.
- `tools/chatgpt/auth.mjs:140` - `export function createAuth(store,`.
- `tools/chatgpt/transport.mjs:4` - `export async function listModels(auth, signal, fetchImpl = fetch)`.
- `tools/chatgpt/transport.mjs:14` - `export function responsePayload(messages, model)`.
- `tools/chatgpt/transport.mjs:37` - `export async function consumeSSE(response,`.
- `tools/chatgpt/transport.mjs:103` - `export async function generate(auth, options, fetchImpl = fetch)`.
- `tools/chatgpt/store.mjs:7` - `export class CredentialStore`.

## Sửa lỗi CHATGPT_EMPTY_RESPONSE sau lần thử live

Lần thử người dùng cung cấp đã tới response.completed nhưng bộ đọc cũ chỉ dùng text trong output của sự kiện cuối.
Code cũ thu delta nhưng bỏ qua chúng khi output cuối rỗng, nên có thể báo CHATGPT_EMPTY_RESPONSE dù text đã stream.
Đã sửa: giữ text từ output_text.delta, output_text.done/content_part.done/output_item.done; loại trùng done events;
chỉ trả text sau response.completed và kiểm tra tính nhất quán. Vẫn lỗi nếu thực sự không có text, incomplete/failed/refused,
stream đứt hoặc vượt giới hạn. Output Judge vẫn qua toàn bộ validation, không tạo nhãn giả.

Telemetry mới giữ requested_model, returned_model, stop_reason, http_status và stream_summary kể cả khi provider lỗi.
stream_summary chỉ có số sự kiện, số ký tự và nguồn ghép text; không lưu text/prompt/token/reasoning.
Test regression đã tái hiện trường hợp delta có text nhưng terminal output rỗng/missing/null, done events lặp,
completion rỗng thật và incomplete. Toàn suite sau sửa: 50 Python + 21 Node đều đạt.
Raw stream của lần lỗi cũ không được lưu nên chưa chứng minh đó chính là nguyên nhân của lần lỗi đã báo;
chưa tự gọi lại inference trên tài khoản người dùng. Chạy lại `judge-chatgpt.cmd Live`, không cần đăng nhập lại nếu phiên còn hợp lệ.
Nếu còn lỗi, telemetry mới sẽ phân biệt text đã được stream với trường hợp server không trả text.
