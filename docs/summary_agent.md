# Summarize Agent của TilLegal

Ngày triển khai: 01/11/2026. Đây là implementation Summary độc lập phục vụ **hỏi đáp (QA)**.
Output là bản nháp chưa kiểm chứng, không phải ý kiến pháp lý đã xác minh.

## 1. Phạm vi, điểm xuất phát và đối chiếu thiết kế

Triển khai trong checkout `D:\Trang\TilLegal (Legal AI)\tillegal-rag-judge-summary`,
nhánh `feature/judge-agent-api-providers`, base `71301af5ad7b57cc9de9da33eb76cf7b73bd0e9e`.
Đã kiểm tra working tree sạch trước khi sửa; commit trùng head của
[nhánh tham chiếu](https://github.com/Legally-Illigal/tillegal-rag/tree/feature/judge-agent-api-providers)
tại thời điểm đọc. Không tìm thấy AGENTS.md trong checkout, các thư mục cha đã kiểm tra hoặc cây nhánh remote.
Checkout `D:\Trang\TilLegal (Legal AI)\Tilligal-judge-agent` có thay đổi chưa commit trên nhánh khác;
không sửa, reset, checkout, push, merge hoặc triển khai dịch vụ ở đó.

Đã đọc trực tiếp PDF `PIPELINE_LEGAL_AI (1).pdf`, mục 6, 6.1, 6.2, 10, 12:
Summary nhận câu hỏi, evidence, Judge status/gaps; tạo draft/claims/citations/version để Verify tương lai đọc nguồn.
PDF được dùng như tài liệu thiết kế. Không thực thi các chỉ dẫn trong dữ liệu nguồn.
Yêu cầu hiện tại giới hạn ở module độc lập và demo tuần tự một lượt; không dựng LangGraph dù đó là
định hướng điều phối của toàn project.

## 2. Vấn đề Summary giải quyết và ranh giới trách nhiệm

BasicRAG generation cũ trả ba trường `draft_answer`, `candidate_evidence_ids`, `abstain`.
Các ID này gắn với toàn câu trả lời, chưa biểu diễn rõ một claim dùng citation nào. Summary mới tạo
các đơn vị trả lời, ghép từng claim với các citation do code cấp, giữ metadata và locator để có thể audit.
BasicRAG và contract ba trường được giữ nguyên để làm baseline.

| Thành phần | Quyết định/thao tác | Không chứng minh được |
| --- | --- | --- |
| Search/BM25 | Tìm và xếp hạng đoạn nguồn | Claim đúng hay luật còn hiệu lực |
| EvidenceBuilder | Lắp quote nguyên vẹn và context groups, ghi omissions | Context đã đủ về pháp lý |
| Judge | Evidence có đủ để xử lý các issues không | Từng citation của câu trả lời đã được Verify |
| Summary | Viết bản nháp từ evidence được phép; tạo mapping bằng code | Semantic faithfulness của claim-citation |
| Verify tương lai | Sẽ đọc đúng nguồn để kiểm chứng nội dung/vị trí/thời điểm | Chưa có implementation trong phần việc này |

Summary không truy xuất nguồn, không tự tăng ngân sách, không gọi Judge bên trong, không sinh Query Agent.
Module không nhận mode kiểm chứng văn bản để sửa claim gốc người dùng; `origin` luôn là `draft`.

## 3. Luồng đang chạy

```text
Caller cung cấp question + query_plan.issues + as_of_date
  -> retrieval BM25 hiện có (một lần)
  -> EvidenceBuilder hiện có
  -> adapter Judge -> JudgeAgent.evaluate() (một lần)
  -> make_summary_input()
  -> SummaryAgent.summarize()
  -> DRAFT_UNVERIFIED / ABSTAINED_UNVERIFIED / ERROR
```

`run_single_pass()` nằm trong CLI nhưng là hàm Python gọi được trực tiếp; không móc private attribute BasicRAG.
Judge chạy một lần đánh giá, không có retry hay search loop. Nếu evidence rỗng, Judge có thể trả bằng code
và không gọi LLM. `stop_reason=single_pass_completed`; không giả lập rằng đã dùng hết ngân sách tìm kiếm.
Query plan là dữ liệu caller cung cấp; fixture minh họa có chủ đích, chưa có Query Agent.

Summary độc lập chỉ cần input JSON đã lưu: không import/call retrieval trong `summarize()`.
Một node LangGraph sau này có thể tạo cùng input rồi gọi phương thức này.

## 4. Logic cốt lõi: code làm gì, LLM làm gì?

1. **Code** kiểm tra kích thước request, schema, ngày, request IDs, bundle fingerprint,
   quote/chunk identity, locators, source policy, các tham chiếu và revision.
2. **Code** dùng `adapt_evidence_bundle()` tạo đúng projection mà validator Judge yêu cầu,
   rồi gọi validator output Judge. Không gọi lại model Judge. Các trường history không có trong
   Summary contract được đặt `search_rounds_completed=0`, `query_history=[]` ở projection kiểm tra;
   đây không phải dữ liệu mô tả một lượt Search mới.
3. **Code** lấy đúng `usable_evidence_ids`, chiếu quote/metadata và context liên quan.
4. **Code** xử lý Judge ERROR/evidence rỗng; map context gaps theo group members và issue_checks,
   không dùng group thiếu làm điều kiện abstain toàn bộ.
5. **LLM** đề xuất units: text, issue IDs, evidence IDs, limitations, abstain.
6. **Code** parse JSON nghiêm ngặt, từ chối key trùng, ID lạ/trùng, ID ngoài tập cho phép,
   issue không có evidence mapping, dấu citation tự sinh và URL tự sinh trong unit text.
7. **Code** cấp C/R IDs, copy metadata nguồn, render draft từ chính các units đã validate,
   gộp limitations và kiểm tra lại public output.

Kiểm tra issue mapping chỉ bảo đảm mỗi issue của unit có giao với evidence trong Judge check;
không chứng minh mọi evidence hỗ trợ mọi từ của unit. Tách một câu thành claim nguyên tử và giữ ngoại lệ
vẫn là yêu cầu trong prompt, cần human review/Verify để đánh giá ngữ nghĩa.

## 5. Công nghệ và provider thực sự dùng

Code core dùng Python stdlib: dict/JSON, dataclass limits, SHA-256 và validators theo convention repo.
Không thêm Pydantic, vector database, agent framework, SDK hoặc dependency bắt buộc cho chạy offline.
Demo tái sử dụng parser/BM25/EvidenceBuilder/retrieval factory có sẵn.

Đường live Summary hỗ trợ **Gemini Developer API** qua `GeminiProvider` và `google-genai==2.34.0`
trong `requirements-gemini.txt` hiện có. Chọn Gemini vì repo đã có transport, timeout, error mapping,
usage metadata, live opt-in và SDK/HTTP tests. Chỉ bổ sung tham số `output_schema` cùng phép chiếu schema;
giá trị mặc định vẫn là schema Judge. Constructor copy schema để không giữ dict của caller.

Summary CLI hỗ trợ Gemini và ChatGPT. OpenAI API và Anthropic có lớp ràng buộc output khác nên
Summary từ chối các provider có sẵn này trước I/O, thay vì chỉ đổi prompt rồi gửi nhầm schema.
ChatGPT tái sử dụng `ChatGPTProvider`/Node bridge hiện có: OAuth từ đăng nhập do người dùng thực hiện,
system prompt đi vào instructions; không gắn schema Judge/BasicRAG ở wire. Summary schema nằm trong
prompt và được validate bằng code sau response.completed; không tuyên bố server-side structured decoding.
Không dùng GEMINI_API_KEY/OPENAI_API_KEY cho đường này, không gửi max_output_tokens mà bridge không hỗ trợ.
Caller có thể viết provider tùy chỉnh có `generate(messages)`; caller chịu trách nhiệm timeout/wire schema,
Summary vẫn validate output cục bộ. Chưa tuyên bố các provider tùy chỉnh đã tương thích.

`FixtureSummaryProvider` replay response chỉ định, không suy luận ngữ nghĩa. Mock CLI yêu cầu fixture rõ;
không tự tạo nhãn đủ hoặc câu trả lời có vẻ hợp lý.

## 6. Input contract đầy đủ

Contract v1 dành riêng QA; không yêu cầu WorkflowState. Mọi field dưới đây đều bắt buộc, các field
nullable phải truyền `null` khi chưa có. Không chấp nhận field ngoài envelope.

| Field | Kiểu/nguồn | Quy tắc |
| --- | --- | --- |
| `schema_version` | string, caller | `"1"` |
| `request_id` | string <=256, caller | Trùng bundle và previous draft/report nếu có |
| `question` | string <=10000 | Câu hỏi nguyên bản, không thay bằng query retrieval |
| `as_of_date` | YYYY-MM-DD hoặc null | Ngày hợp lệ; null tạo limitation, không tự suy đoán |
| `query_plan` | `{issues:[{issue_id,text}]}` | 1-64 issues, IDs duy nhất; caller cấp |
| `evidence_bundle` | Output thật EvidenceBuilder v1 | Giữ bundle nguyên vẹn, không sửa hash để làm hợp lệ |
| `judge_result` | Output thật Judge | status/checks/usable/gaps/queries/explanation/error_code đúng schema hiện tại |
| `stop_reason` | string <=256 hoặc null | Demo dùng `single_pass_completed` |
| `draft_version` | integer >=1 | Lần đầu 1; revision đúng version cũ +1 |
| `previous_draft` | Public Summary output hoặc null | Bản nháp trước của cùng request/as_of_date |
| `verification_report` | Handoff revision v1 hoặc null | Cùng request và version bản cũ, references hợp lệ |

`make_summary_input()` là adapter thuần, copy sâu dữ liệu, không mutate, không retrieval và không
tự đánh giá sufficiency. Caller vẫn cần gọi `summarize()` hoặc `validate_input()` để kiểm tra.

### Bundle thực tế đã đối chiếu

Envelope giữ `schema_version`, `builder_version`, `request_id`, `query`, `index_id`, `bundle_id`,
`source`, `corpus_snapshot_id`, `usage_policy`, `approved_for_legal_verification`, `retrieval_status`,
`status`, `budget`, `quote_chars`, `evidence`, `groups`, `limitations`, `trace`.
Các bổ sung retrieval v2 như `retrieval_method`/`retriever_id` được giữ; Summary không diễn giải score
thành độ tin cậy pháp lý. `EXPERIMENTAL_ONLY`, `approved_for_legal_verification=false` là policy hiện tại.

Trong mỗi evidence, nội dung nằm ở **`quote`**, không phải `content`; Judge adapter đổi tên thành
`content` ở projection prompt. `document` chứa metadata, URL nằm ở **`document.source_url`**;
`chunk` chứa `article`, `clause`, `point`, `scope`, `appendix`, `form` và normalized text.
Evidence có `document_id`, `document_version_id`, `chunk_id`, `article_id`, `quote_sha256`,
`quote_representation`, `normalized_locator`, `source_locator`, ancestry và các warning/status flags.

Group giữ retrieved seed, rank, retrieval score/cautions, `members=[{evidence_id,role}]`,
`omitted_chunk_ids`, `seed_included`, `article_expansion_allowed`, `context_sufficiency`.
Summary giữ nguyên các cảnh báo: parser candidate, attachment chưa review, table cần raw layout,
quoted address là outer context, headings cần body... Chúng không bị đổi thành verified.

Validation dùng bundle fingerprint từ toàn payload bỏ `bundle_id`, kiểm tra quote hash và chunk text,
identity nhất quán document/chunk/evidence, doc metadata cùng version không xung đột, locator đúng chunk,
quote budget và group references. Đây là kiểm tra toàn vẹn cấu trúc, **không xác thực cơ quan ban hành**.
Không có đủ raw source để tự xác minh nội dung HTML, provenance hoặc hiệu lực pháp luật.

JSON Schema xuất ra mô tả envelope và fields public. Metadata document/chunk ở schema được giữ như object;
validator Python bổ sung các invariants cụ thể nêu trên và validator Judge kiểm tra projection.
Chỉ chạy một JSON Schema validator bên ngoài sẽ chưa thay thế các kiểm tra bằng Python.

## 7. Bundle và Judge result bổ sung cho nhau

Bundle trả lời: có đoạn nguồn nào, text/metadata/locator gì, context nào bị thiếu.
Judge result trả lời: issue nào có căn cứ và bằng chứng nào được phép dùng, còn thiếu gì.
Không thể chỉ truyền Judge result cho Summary vì thiếu nội dung nguồn; cũng không thể chỉ nhìn
bundle `ASSEMBLED` rồi kết luận đủ vì đây chỉ là trạng thái lắp context.

Summary chỉ đưa quote thuộc tập Judge usable vào payload. Giữ các groups có giao với tập đó và warnings.
Policy cũ **complete relevant groups** abstain toàn bộ nếu group liên quan có omitted chunk, seed chưa
include hoặc member ngoài usable. Policy này đã được thay ngày 02/11/2026 bằng
**ISSUE_SCOPED_PARTIAL_GROUNDED_ANSWERS**: một group thiếu chỉ giới hạn ý dùng evidence trong group đó;
issue/evidence độc lập vẫn được xử lý. Không gửi quote chưa được chọn và không tự fetch đoạn thiếu.

`select_context()` trả selected evidence, context và `context_gaps` có cấu trúc (không còn global blockers).
Mỗi gap có `gap_id`, `group_id` (retrieved chunk ID), `seed_chunk_id`, `kind`, `issue_ids`,
`evidence_ids` được chọn bị ảnh hưởng, `related_evidence_ids` của group, `omitted_chunk_ids`,
`non_usable_evidence_ids`, `scope_status`, `statement_policy`.
Kind tách riêng OMITTED_CHUNKS / NON_USABLE_MEMBERS / SEED_NOT_INCLUDED. Ba trạng thái có thể cùng
xuất hiện, không nhập chung thành kết luận rằng nguồn sai. Gap IDs G1... ổn định theo thứ tự groups/kinds,
chỉ có ý nghĩa trong request/version tương ứng, không là citation IDs.

Code lấy tập member và seed có định danh thực trong bundle, giao với evidence_ids của từng Judge check
để map issues. `evidence_ids` của gap là các selected members/seed liên quan; không dùng vị trí chunk,
role, score hay field name để suy đoán gap quan trọng về ngữ nghĩa. Không map được issue thì giữ
GROUP_ONLY_UNKNOWN_ISSUES, không tự gán gap cho mọi issue. Nhóm này không cấp thêm nội dung nguồn cho model.
Group members trong prompt được lọc theo usable; excluded IDs chỉ xuất hiện như metadata cảnh báo.

Để không biến mọi blocker thành warning rồi cho phép mọi kết luận, code yêu cầu `statement_type=source_statement`
cho unit dùng evidence có gap **hoặc** issue có coverage partial/none. `application` hoặc thiếu khai báo trong
những trường hợp này trả SUMMARY_CONTEXT_SCOPE_REQUIRED (lỗi contract, không phải evidence thiếu).
Với issue full và evidence không bị gap ảnh hưởng, có thể khai báo application khi chính nội dung nguồn
đủ hỗ trợ kết luận. Phần kiểm tra này chỉ ràng buộc khai báo phạm vi; model vẫn có thể gắn nhãn source_statement
cho câu không đúng nghĩa. Prompt yêu cầu chỉ mô tả nội dung nguồn xác định được, không kết luận áp dụng nếu
điều kiện/ngoại lệ thiếu có thể đổi kết luận, bỏ unit không thể viết độc lập và vẫn viết các unit khác.
Đây là kiểm soát bằng code về scope + prompt về nội dung, **chưa phải semantic verification**.
Cross-reference chưa được Builder mở rộng vẫn là limitation. Không khẳng định độ chính xác giữ nguyên.

## 8. Hai lớp output

LLM trả đúng ba field, không tự cấp IDs hoặc metadata:

```json
{
  "units": [
    {"text": "Khẳng định từ nguồn giả định.", "issue_ids": ["I1"], "evidence_ids": ["E1"], "statement_type": "source_statement"}
  ],
  "limitations": [],
  "abstain": false
}
```

Ví dụ E1 ở trên là alias để đọc, không phải ID thực của Builder. Không dùng JSON này làm fixture
cho bundle thật. Giới hạn: <=64 units; text mỗi unit <=6000 ký tự; <=64 issue refs và <=32 evidence refs
mỗi unit, không trùng; <=64 model limitations, mỗi chuỗi <=4000 ký tự. `statement_type` có hai giá trị
source_statement/application; optional cho units không bị giới hạn để tương thích output cũ, nhưng bắt buộc
source_statement khi issue partial/none hoặc unit dùng evidence chịu gap. `abstain=true` đòi units rỗng
và limitations có lý do; false cần units không rỗng. Không chấp nhận Markdown fence, JSON key trùng,
dấu `[`/`]` hoặc `http(s)://`/`www.` trong unit text. Các số điều viết trong prose vẫn cần kiểm tra ngữ nghĩa.

Public Summary output có cùng envelope cho cả ba trạng thái:

| Field | Thành công | Abstention | Lỗi |
| --- | --- | --- | --- |
| `schema_version` | `"1"` | `"1"` | `"1"` |
| `request_id`, `as_of_date`, `stop_reason` | Phản ánh input | Phản ánh input | Giá trị hợp lệ lấy được, còn lại null |
| `draft_version` | Version caller cấp | Version caller cấp | Version hợp lệ lấy được hoặc null |
| `status` | `DRAFT_UNVERIFIED` | `ABSTAINED_UNVERIFIED` | `ERROR` |
| `answer_draft` | Text render từ claims | null | null |
| `claims`, `citations` | Có mapping | Mảng rỗng | Mảng rỗng |
| `limitations` | Đã gộp bằng code | Gồm lý do abstain | Giới hạn đã thu thập, tối thiểu chưa Verify |
| `error` | null | null | `{code,stage}` |

Các field bổ sung của output mới (optional trong schema để nhận previous_draft v1 cũ, nhưng khi có
phải có đủ cả bốn và được validate bằng code):

- `context_gaps`: tối đa 300 gaps có schema nêu ở mục 7, ID unique, issue references và các tập ID nhất quán.
- `issue_reports`: tối đa 64, gồm issue_id/text, **judge_coverage** copy nguyên full/partial/none,
  **summary_status** HAS_DRAFT_CLAIMS/NO_DRAFT_CLAIMS, claim_ids, context_gap_ids và limitations.
  Có claim không được hiểu là Summary đã bao phủ toàn issue; Summary không tự sửa coverage của Judge.
- `unscoped_limitations`: missing_items hoặc model limitations chưa gắn issue chắc chắn.
  Nhận ID đã biết ở đầu dòng: `I1: ...`, `I1 — ...`, `issue_id: I1 — ...`, `ISSUE_ID = I1: ...`,
  có thể có khoảng trắng/bullet, dấu hai chấm fullwidth hoặc dấu gạch khác nhau. Không đoán từ ID được
  nhắc giữa câu; ID lạ (I10 khi chỉ có I1) hoặc prefix nhiều issue mơ hồ vẫn giữ ở unscoped.
- `display_response`: phần UI/CLI **nên hiển thị**, gồm answer_draft, giới hạn theo issue, khoảng trống chưa
  map issue và nhắc draft chưa Verify. `answer_draft` vẫn chỉ là các claim + citation markers cho Verify sau này.

`display_response` là trường response công khai, không phải trace. Lấy riêng answer_draft để hiển thị sẽ
bỏ mất phần giới hạn và không phải cách tích hợp UI được khuyến nghị. Public validator kiểm tra report refs,
scope của claim và text render hiển thị nhất quán; không kiểm chứng support ngữ nghĩa.

Cập nhật hiển thị 02/11/2026: output mới có `display_version="2"`. UI gom theo tiêu đề vấn đề,
dịch coverage thành mô tả tiếng Việt, bỏ mã G1/G2, chunk IDs, enum như NON_USABLE_MEMBERS hoặc
HAS_DRAFT_CLAIMS khỏi phần văn bản người dùng. Mỗi loại gap chỉ giải thích một lần trong từng issue,
không lặp một dòng cho mỗi group. Cảnh báo nhóm chưa map issue được gom thành một lời nhắc chung.
Metadata flags có trong câu giới hạn được dịch bằng bảng từ vựng cố định; JSON nguồn giữ nguyên.

Raw Judge/model limitations vẫn giữ trong `limitations`/`issue_reports`/`unscoped_limitations` và
chi tiết gap vẫn nằm ở `context_gaps`; chỉ phần display bỏ prefix và gộp các dòng trùng sau chuẩn hóa
Unicode, khoảng trắng, hoa/thường và dấu chấm kết thúc. **Không fuzzy-merge** các câu khác về số liệu,
phủ định, điều kiện hoặc cách diễn đạt, vì có thể làm mất thông tin. Prompt yêu cầu model chỉ thêm
giới hạn mới, không chép/diễn đạt lại cùng missing_items của Judge. Cách này giảm lặp ở live output
nhưng không bảo đảm phát hiện mọi cặp paraphrase; vẫn cần kiểm tra nội dung thực tế.

Saved draft cũ không có display_version vẫn được validate bằng renderer cũ để revision không bị
từ chối vì chỉ đổi cách trình bày. Draft version, claims/citations và Judge coverage không thay đổi.
Kiểm thử sau sửa attribution/display: **149 tests Python đạt**, trong đó **60 tests Summary**.
Bao gồm nhãn issue biến thể, ID lạ/nhiều issue mơ hồ, dedup giữa Judge/model sau chuẩn hóa,
giữ số liệu/phủ định khác nhau, ẩn mã kỹ thuật nhưng giữ raw JSON và revision từ display cũ.
Không gọi API live trong quá trình sửa; chất lượng tránh lặp paraphrase cần xem lại bằng live/human review.

Tách lớp giúp LLM tập trung tổng hợp, code chịu trách nhiệm định danh/provenance/render và ràng buộc
cấu trúc. JSON hợp lệ và ID tồn tại không làm draft thành VERIFIED.

## 9. Claim, citation và provenance

Code cấp C1, C2... theo thứ tự units. Citation R1, R2... theo **lần gặp evidence đầu tiên** khi duyệt
units/evidence_ids. Cùng evidence được reuse một R trong version; nhiều evidence tạo nhiều R cho một claim.
ID ổn định với cùng thứ tự units và references, không bảo đảm ổn định nếu LLM đổi thứ tự.

```text
(request_id, draft_version, C1)
   -> citation_ids R1, R2 trong cùng version
   -> evidence_id + bundle_id
   -> document_id + document_version_id + chunk_id
   -> quote/hash + normalized locator + raw source locator + source metadata
```

Citation copy `source_url` từ `document.source_url`, vị trí từ `chunk.article/clause/point`.
Giữ `article_id_status`, `attachment_status`, `address_status`, `exact_citation_verified=false`,
`legal_evidence_status`, `context_review_required`, warnings và `citation_status=CANDIDATE_UNVERIFIED`.
`source_metadata` copy toàn document metadata, gồm raw_ref, provenance, ngày hiệu lực có thể null;
`chunk_metadata` copy chunk để giữ scope/ancestry IDs và dấu vết parser. Quote không bị cắt.

`normalized_locator` là offsets trong normalized block, Unicode code points, end-exclusive.
`source_locator` trỏ whole raw block, không tự nhận rằng offsets đó là vị trí chính xác của đoạn quote
trong HTML gốc. `source_metadata.raw_ref` giúp quay về record; future Verify vẫn cần kho raw tương ứng.

`document_version_id` là fingerprint nội dung/metadata bản dữ liệu, **không phải chứng cứ phiên bản luật
có hiệu lực tại `as_of_date`**. QUARANTINE/UNVERIFIED/UNKNOWN giữ nguyên, metadata null không được đoán.

Rendered draft nối từng `claim.text` với markers do code tạo, rồi phân cách bằng dòng trống.
Không cho LLM tự chèn markers. Public validator kiểm tra claims/citations/render khớp nhau.

## 10. Judge status, abstention và limitations

- SUFFICIENT: cho sinh từ evidence usable với scope theo từng group; output vẫn chưa Verify.
- INSUFFICIENT: có thể viết phần có căn cứ hoặc abstain; code giữ `JUDGE_INSUFFICIENT` và mọi missing_items.
- ERROR: `SUMMARY_JUDGE_ERROR`, không gọi Summary provider; lỗi gốc còn trong input Judge/trace demo.
- Bundle/usable rỗng: abstain với `NO_USABLE_EVIDENCE`, không gọi provider.
- Context group thiếu: vẫn gọi provider khi có usable evidence; source_statement-only cho evidence/issue
  bị ảnh hưởng, không cho kết luận áp dụng dựa trên phần context chưa rõ. Không chặn các ý độc lập.
- Abstain toàn bộ khi không có usable evidence hoặc model xác định không có unit độc lập có căn cứ.
  Không dùng disclaimer 'chưa đầy đủ' để hợp thức hóa claim thiếu căn cứ; coverage partial chỉ mô tả issue.
- Lỗi JSON/schema/timeout/provider: ERROR; không biến lỗi kỹ thuật thành evidence thiếu.

Code gộp và loại trùng limitations từ: cờ chưa Verify cố định, bundle limitations/policy,
retrieval cautions/omissions, Judge missing_items/status, source warnings, tình trạng approval/provenance/
rights/time của từng evidence, as_of_date null, stop_reason và limitations của LLM.
Thông tin metadata thời gian chưa đủ luôn có `APPLICABILITY_AT_AS_OF_DATE_NOT_VERIFIED`; không suy ra hiệu lực.
Model limitations được yêu cầu chỉ mô tả khoảng trống; lọc kết luận pháp lý trá hình trong free text
vẫn cần human review, chưa có semantic detector.

## 11. Revision handoff cho Verify tương lai

Đây là **contract đề xuất**, không phải output của một Verify đã triển khai:

```json
{
  "schema_version": "1",
  "request_id": "summary-fixture",
  "draft_version": 1,
  "claim_reports": [
    {
      "claim_id": "C1",
      "citation_ids": ["R1"],
      "action": "needs_evidence",
      "reason": "REPORT GIẢ ĐỊNH: cần căn cứ bổ sung cho ngoại lệ."
    }
  ]
}
```

Actions: `keep`, `revise`, `remove`, `needs_evidence`. `citation_ids` có thể rỗng khi nhận xét toàn claim;
nếu có phải thuộc claim đó của draft cũ. Mỗi claim tối đa một report; có thể báo một phần claims.
Không truyền tự do một schema Verify tưởng tượng có nhãn khác; future adapter cần chuyển đúng handoff này.

Revision truyền previous_draft cùng report version cũ và `draft_version=old+1`. Validator kiểm tra
request, as_of_date, previous output shape/render, issue IDs và mọi claim/citation refs.
Cho revision từ DRAFT hoặc ABSTAINED; previous ERROR bị từ chối. Với abstention không có claim,
report có claim_reports rỗng. Caller quản lý history/version, Summary không sửa chúng.

Prompt revision có old draft text/claims và report nhưng **không cấp lại quote/nguồn cũ qua citations**.
Chỉ nguồn trong current usable evidence được làm căn cứ. Action remove có guard từ chối lặp nguyên text;
guard không phát hiện mọi paraphrase. Revise/needs_evidence dựa vào LLM, cần audit lại.
Nguồn cần thêm mà input chưa có: loại phần thiếu, nêu hạn chế hoặc abstain, không tự Search.
Version mới vẫn DRAFT_UNVERIFIED (hoặc abstain/error), report cũ không bị đổi nhãn và không được gắn
lại làm kết quả Verify cho version mới. Reference bên ngoài luôn dùng request + version + local ID.

## 12. Prompt, giới hạn context và lỗi

System prompt tiếng Việt ở `summary_prompt.py` chứa quy tắc dùng evidence, giữ điều kiện/ngoại lệ/thời điểm,
abstention, revision, không tuyên bố đã xác minh và structured schema. Mọi input không đáng tin nằm ở
user message. Đây là biện pháp phân tách chỉ dẫn/dữ liệu, không phải bảo đảm chống injection tuyệt đối.

SummaryLimits mặc định: request JSON <=2.000.000 bytes; messages UTF-8 <=100.000 bytes;
raw model output <=24.000 ký tự. Có thể cấu hình trong các ngưỡng được validator công bố.
Không cắt quote âm thầm. Vượt request/prompt/output trả `SUMMARY_REQUEST_LIMIT`, `SUMMARY_INPUT_LIMIT`,
`SUMMARY_OUTPUT_LIMIT`. Giới hạn byte không tương đương token budget.

Gemini live: `--mode live --allow-live`, key/model từ môi trường, không .env discovery, không OAuth.
Tái sử dụng timeout transport, một attempt, không fallback model; default 30 giây, output cap 4096 tokens.
Timeout mạng/API có mã GEMINI_TIMEOUT; SDK trả incomplete/safety/quota/dependency errors riêng.
Timeout transport không phải hard deadline toàn pipeline; caller tương lai phải quản lý deadline tổng.
SummaryAgent cũng phân loại TimeoutError của custom provider và không tự retry.

Telemetry được truyền bằng dict request-local: prompt_version, model/provider_mode, provider_calls,
input_bytes, stage, error_code, latency_ms. `latency_ms` ngoài cùng đo toàn `summarize()`, nested provider
latency đo transport; không cộng hai số chồng lấn. Usage có khi provider trả. Chỉ copy metadata allowlist,
không lưu API key, raw exception, raw response hay prompt trong telemetry. Input/artifact JSON vẫn chứa
câu hỏi và evidence: caller tự quản lý nơi lưu, quyền đọc và retention.

## 13. File, interface và nhiệm vụ

| File | Class/function | Vai trò |
| --- | --- | --- |
| [summary_contracts.py](../ai_core/summary_contracts.py) | SummaryLimits, validate_input, validate_bundle, validate_llm_output, validate_public_output, json_schemas | Shape, identity, revision và lỗi |
| [summary_adapter.py](../ai_core/summary_adapter.py) | make_summary_input, select_context, collect_limitations | Projection thuần và policy context |
| [summary_prompt.py](../ai_core/summary_prompt.py) | summary_messages, SYSTEM_PROMPT | Prompt Việt/version, byte limit |
| [summary_display.py](../ai_core/summary_display.py) | parse_issue_limitation, render_readable_response | Nhận nhãn issue tường minh, gộp trùng định dạng và hiển thị giới hạn tiếng Việt |
| [summary_agent.py](../ai_core/summary_agent.py) | SummaryAgent.summarize, FixtureSummaryProvider, check_provider_schema | Tối đa một provider call, telemetry riêng |
| [draft_renderer.py](../ai_core/draft_renderer.py) | render_draft | Code cấp IDs, copy metadata, render |
| [summary_cli.py](../ai_core/summary_cli.py) | main, configured_provider, run_single_pass | Saved-input/adapt/demo/schemas, JSON UTF-8 |
| [gemini_provider.py](../ai_core/gemini_provider.py) | GeminiProvider, gemini_output_schema | Thêm output_schema tùy chọn; mặc định Judge giữ nguyên |
| [build_fixture.py](../examples/summary/build_fixture.py) | fixture_index, fixture_input, main | Nguồn giả định + index integrity hợp lệ trên runtime hiện tại |
| [test_summary_agent.py](../tests/test_summary_agent.py) | SummaryAgentTests | Contract, mapping, context, revision, lỗi |
| [test_summary_integration.py](../tests/test_summary_integration.py) | SummaryIntegrationTests, SummaryGeminiWireTests | CLI/demo/Baseline regression + actual SDK với HTTP giả lập |
| [test_summary_partial_policy.py](../tests/test_summary_partial_policy.py) | PartialPolicyTests | Isolate groups/issues, scope guard, visible gaps, excluded IDs và report integrity |
| [partial_review.py](../examples/summary/partial_review.py) | partial_case, main | Nguồn giả định có 2 issues và 3 kịch bản human review |
| [schemas/](../examples/summary/schemas/) | 4 JSON Schemas | Input, model output, public output, revision report |

README thêm điểm vào báo cáo này; `.gitignore` chỉ mở hai test Summary để chúng được đưa vào handoff
và bỏ qua môi trường `.venv-summary` tùy chọn. Không thay requirements của baseline.

Interface tích hợp trực tiếp:

```python
from ai_core.gemini_provider import GeminiProvider
from ai_core.summary_contracts import LLM_OUTPUT_SCHEMA
from ai_core.summary_agent import SummaryAgent

# Chỉ tạo khi caller đã chủ động chọn live; không gọi trong test mặc định.
provider = GeminiProvider.from_environment(allow_live=True, output_schema=LLM_OUTPUT_SCHEMA)
agent = SummaryAgent(provider)
telemetry = {}
result = agent.summarize(value, telemetry=telemetry)
```

## 14. Ví dụ JSON xuyên suốt, insufficient và revision

Nguồn fixture hoàn toàn bịa để test kỹ thuật: khoản 1 nêu điều kiện B cho đối thoại nhóm A;
khoản 2 là ngoại lệ C. URL `https://example.invalid/summary-fixture` không phải nguồn pháp luật.
Generator dùng DocumentVersion/parser/BM25/Builder thật, vì vậy IDs và locators là dữ liệu thực của
nguồn **giả định**, không hard-code metadata nguồn vào renderer.

Chạy generator bên dưới tạo đầy đủ JSON có thể nạp trực tiếp:

| Artifact | Nội dung |
| --- | --- |
| `context.json` | request, question, issues do caller cung cấp, date |
| `bundle.json` | Quote đầy đủ, document/chunk metadata và groups |
| `judge_result.json` | **Fixture** SUFFICIENT có chủ đích, không phải LLM Judge |
| `input.json` | Input Summary hoàn chỉnh ghép đúng bundle/Judge |
| `model_output.json` | Fixture một unit, references lấy từ bundle |
| `output.json` | Draft C1 -> R1 -> evidence -> document/chunk |
| `insufficient_input.json` | Judge partial, missing_items về căn cứ thời gian |
| `revision_report.json` | Report giả định cho C1/R1, draft 1 |
| `revision_input.json` | previous_draft + report + draft_version 2 |

Mock một unit chỉ minh họa phần quy tắc khoản 1, chưa bao phủ ngoại lệ; generator không tự tuyên bố
fixture có chất lượng trả lời đầy đủ. Đây là ví dụ vì sao structural success không phải semantic success.
Human reviewer cần yêu cầu câu trả lời giữ ngoại lệ và gắn thêm citation cho khoản 2 trước khi dùng thực tế.
Report giả định yêu cầu bổ sung; replay cùng fixture ở revision chỉ chứng minh version/mapping plumbing,
không chứng minh đã sửa đúng nội dung report.

Ví dụ public output tối giản **chỉ để đọc** (bỏ metadata dài; không phải file để nạp):

```text
status: DRAFT_UNVERIFIED
draft_version: 1
answer_draft: <text của unit> [R1]
claims: C1(text, issue_ids=[I1], citation_ids=[R1], origin=draft)
citations: R1(evidence_id thật, source_url copy từ document, locators và metadata nguyên bản)
limitations: DRAFT_NOT_VERIFIED, source/time UNKNOWN, fixture disclaimer, ...
error: null
```

Insufficient example vẫn trả draft nếu còn usable/context đầy đủ, đồng thời có
`JUDGE_INSUFFICIENT` và `Chưa có căn cứ hiệu lực tại thời điểm yêu cầu.` trong limitations.
Revision output version 2 vẫn unverified, local C1/R1 thuộc version 2 chứ không ghi đè draft 1.

## 15. Cài và chạy trong PowerShell

Từ checkout này. Nếu đã có Python 3.14 đi kèm `.tmp`, dùng đúng executable đó như kiểm thử thực tế:

```powershell
Set-Location -LiteralPath 'D:\Trang\TilLegal (Legal AI)\tillegal-rag-judge-summary'
$py = '.\.tmp\python314\python.exe'
& $py --version
```

Offline core/BM25 fixture không cần Gemini SDK hoặc baseline torch. Máy khác có thể dùng Python >=3.11;
index export/load ràng buộc Unicode/code implementation, nên tạo fixture mới trên runtime đang dùng.
Nếu cần môi trường riêng có SDK cho live:

```powershell
py -3.14 -m venv .venv-summary
$py = '.\.venv-summary\Scripts\python.exe'
& $py -m pip install -r requirements-gemini.txt
```

Tạo nguồn/index/JSON fixture trong **thư mục mới**:

```powershell
$fixture = 'outputs/summary-fixture-' + [guid]::NewGuid().ToString('N')
& $py -B -m examples.summary.build_fixture --output-dir $fixture
```

Summary độc lập offline, không chạy lại Search/Judge:

```powershell
& $py -B -m ai_core.summary_cli summarize --input "$fixture/input.json" --mock-response "$fixture/model_output.json" --output "$fixture/standalone.json" --telemetry "$fixture/standalone-telemetry.json"
& $py -B -m ai_core.summary_cli summarize --input "$fixture/insufficient_input.json" --mock-response "$fixture/model_output.json" --output "$fixture/insufficient-output.json"
& $py -B -m ai_core.summary_cli summarize --input "$fixture/revision_input.json" --mock-response "$fixture/model_output.json" --output "$fixture/revision-output.json"
```

Demo tuần tự offline với **cả Judge và Summary đều là fixture**, retrieval/Builder thật:

```powershell
$run = 'outputs/summary-demo-' + [guid]::NewGuid().ToString('N')
& $py -B -m ai_core.summary_cli demo --context "$fixture/context.json" --index "$fixture/index/manifest.json" --judge-mock-response "$fixture/judge_result.json" --mock-response "$fixture/model_output.json" --artifacts-dir $run
```

Demo hoàn tất lưu 7 JSON: retrieval, bundle, judge_input, judge_result, input Summary, result, telemetry.
Mỗi bước được lưu ngay khi hoàn thành; thư mục run mới được tạo trước inference. Tiến độ in trên stderr
theo retrieval/evidence_builder/judge/summary; stdout vẫn chỉ chứa JSON kết quả. Telemetry cập nhật stage
và status RUNNING/COMPLETED/ERROR/CANCELLED trong chính thư mục mới của lần chạy này.
Ctrl+C trả exit 130, result ERROR có code SUMMARY_CANCELLED và stage đang chạy; giữ mọi checkpoint
đã hoàn thành. Nếu hủy trong Judge sẽ chưa có judge_result/input Summary; không tạo kết quả Judge giả.
Nếu hủy trong Summary thì judge_result và input đã được giữ. Không thể khôi phục file từ các lần chạy
cũ trước bản sửa này nếu đã hủy khi chúng còn chưa được lưu.
Không quảng bá demo này là Judge live. Query plan trong context do caller/fixture cung cấp.

Ghép output Judge đã lưu: cần đúng bundle gốc và caller context cùng request, question/issues/date
của lần Judge đã đánh giá. Validator kiểm tra ID membership/consistency, **không thể chứng minh lịch sử
một file Judge thuần nếu bị ghép với bundle khác dùng trùng IDs**. Caller cần giữ cùng run directory.

```powershell
& $py -B -m ai_core.summary_cli adapt --bundle "$fixture/bundle.json" --judge-result "$fixture/judge_result.json" --context "$fixture/context.json" --output "$fixture/adapted-input.json"
```

Live Gemini dùng môi trường server/process: đặt `GEMINI_API_KEY` và `GEMINI_MODEL` ngoài code; không commit key.
Không có model mặc định được đoán. Chọn model mà project/account thực tế được cấp quyền.

```powershell
# GEMINI_API_KEY và GEMINI_MODEL cần được cấp cho process trước.
if (-not $env:GEMINI_API_KEY -or -not $env:GEMINI_MODEL) { throw 'Cần GEMINI_API_KEY và GEMINI_MODEL.' }
& $py -B -m ai_core.summary_cli summarize --input "$fixture/input.json" --mode live --provider gemini --allow-live --timeout 30 --output "$fixture/live-summary.json" --telemetry "$fixture/live-summary-telemetry.json"
```

Lệnh trên chạy **Summary live**, nhưng Judge trong input fixture vẫn là fixture. Để cả Judge và Summary
chạy live trong demo một lượt, bỏ toàn bộ mock flags và cấp context/index thật tương ứng:

```powershell
$liveRun = 'outputs/summary-live-' + [guid]::NewGuid().ToString('N')
& $py -B -m ai_core.summary_cli demo --context "$fixture/context.json" --index "$fixture/index/manifest.json" --mode live --provider gemini --allow-live --timeout 30 --artifacts-dir $liveRun
```

Đây vẫn là nguồn giả định nếu dùng index fixture. Với dữ liệu project, thay hai đường dẫn context/index.
Nếu Judge ERROR hoặc usable rỗng, Summary không gọi LLM; context gap riêng lẻ không còn chặn toàn pipeline.
Live opt-in không bắt buộc pipeline phải
phát sinh hai inference calls. Chưa chạy các lệnh live trong quá trình triển khai.

Mọi output file phải mới; không tự overwrite. Parent của `--output`/`--telemetry` phải tồn tại.
CLI JSON UTF-8, exit 0 cho draft/abstain; exit 1 cho ERROR; hủy bằng Ctrl+C exit 130;
lỗi parse argument do argparse exit 2.
Lỗi trước agent được in JSON và lưu vào fresh `--output` nếu ghi được; lỗi I/O không thể lưu vẫn ra stdout.

Xuất schemas mới khi cần:

```powershell
$schemaDir = 'outputs/summary-schemas-' + [guid]::NewGuid().ToString('N')
& $py -B -m ai_core.summary_cli schemas --output-dir $schemaDir
```

Index mismatch: CLI giữ diagnostic gốc trên stderr, hiển thị Python/Unicode runtime và hướng dẫn
dùng runtime đã ghim hoặc rebuild từ processed manifest gốc bằng lệnh build của repo.
Không sửa hash, unicode_version hay tắt integrity validator. Index fixture có thể tạo mới bằng generator;
index corpus thật cần processed manifest/snapshot gốc và đúng công cụ build hiện tại.

## 16. Kiểm thử và giới hạn bằng chứng

### Chạy live qua tài khoản ChatGPT (bổ sung)

Từ cùng checkout và biến `$py` ở mục 15; Node.js 22+ phải có trên PATH. Provider này không cần Gemini SDK.
Dùng PowerShell của tài khoản Windows thông thường; phiên đăng nhập/DPAPI gắn với Windows user đó.

```powershell
node --version
& $py -B -m ai_core.judge_cli chatgpt-status
# Nếu chưa connected, chạy đăng nhập bằng trình duyệt:
& $py -B -m ai_core.judge_cli chatgpt-login
& $py -B -m ai_core.judge_cli chatgpt-models
```

Các lệnh tài khoản nằm ở judge_cli vì đó là entrypoint bridge đã có; không chạy Judge inference trong
login/status/models. Đăng nhập cấp quyền cho app tên Tillegal Judge hiện có; Summary dùng chung phiên này.
Model lấy từ catalog account; có thể thêm `--model` với slug đúng do chatgpt-models trả về.
Quyền dùng model/hạn mức phụ thuộc account và kết quả gọi thực tế, không suy ra chỉ từ gói đăng ký.

Summary live độc lập từ input đã lưu (thay đường dẫn bằng input thật của bạn):

```powershell
$id = [guid]::NewGuid().ToString('N')
& $py -B -m ai_core.summary_cli summarize --input "$fixture/input.json" --mode live --provider chatgpt --allow-live --timeout 120 --output "$fixture/chatgpt-summary-$id.json" --telemetry "$fixture/chatgpt-summary-$id-telemetry.json"
```

Nếu dùng fixture/input.json thì nguồn và Judge result vẫn là fixture; chỉ Summary gọi model thật.
Để Judge và Summary đều gọi ChatGPT một lượt, retrieval/Builder hiện có:

```powershell
$chatgptRun = 'outputs/summary-chatgpt-' + [guid]::NewGuid().ToString('N')
& $py -B -m ai_core.summary_cli demo --context "$fixture/context.json" --index "$fixture/index/manifest.json" --mode live --provider chatgpt --allow-live --timeout 120 --artifacts-dir $chatgptRun
Get-Content -LiteralPath "$chatgptRun/result.json" -Raw -Encoding UTF8
Get-Content -LiteralPath "$chatgptRun/telemetry.json" -Raw -Encoding UTF8
```

Với corpus project thay context/index fixture bằng cặp file thật. Không thêm mock flags hoặc
`--max-output-tokens` khi chạy ChatGPT. Telemetry có provider_mode=live, returned_model và
provider.stop_reason=response.completed khi inference hoàn tất; JSON sai vẫn ERROR sau validation.
Đường mới được test bằng bridge giả lập, chưa gọi inference live hay đăng nhập hộ người dùng.

Lỗi thường gặp: CHATGPT_NODE_REQUIRED (Node chưa có PATH), CHATGPT_SIGN_IN_REQUIRED (cần login),
CHATGPT_USAGE_LIMIT (hạn mức tài khoản), CHATGPT_SECURE_STORAGE_ERROR (DPAPI/helper/Windows user),
PROVIDER_TIMEOUT (timeout), SUMMARY_INVALID_OUTPUT_JSON/SCHEMA (model không trả đúng contract).
Không sửa validator để biến output không hợp lệ thành draft hợp lệ.
Đối chiếu [OpenAI Docs: Models and inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference).

Lệnh kiểm tra:

```powershell
& $py -B -m unittest tests.test_summary_agent tests.test_summary_integration -v
& $py -B -m unittest discover -s tests -v
```

Nhóm Summary kiểm tra success/source chain, multi-citation/reuse, unknown/duplicate/nonusable IDs,
mapping sai issue, insufficient gaps, Judge ERROR, empty evidence, incomplete context, metadata null,
policy/provenance/time không nâng cấp, JSON sai/key trùng, giới hạn request/output/units/references,
timeout/provider error, render nhất quán, input không mutate, revision/version/report refs và origin.
Integration chạy parser/BM25/Builder thật, export/load integrity, saved-input/adapt/demo CLI, UTF-8,
live opt-in/no overwrite và BasicRAG regression. Gemini test chạy actual SDK với httpx.MockTransport,
kiểm tra **HTTP payload thực** có `units/limitations/abstain`, không schema ba trường baseline/Judge,
và usage telemetry không chứa fixture key.

Kết quả kiểm tra trên Python **3.14.2**, Unicode **16.0.0**, google-genai **2.34.0**:

- Toàn bộ suite Python sau bổ sung ChatGPT: **126 tests đạt**, bao gồm **37 tests Summary**; không skip ở runtime này.
  Sau sửa checkpoint/progress/Ctrl+C, chạy lại toàn bộ nhóm Summary: **40 tests đạt** (thêm 3 test về
  hủy ở Judge, hủy ở Summary và timeout). Không chạy lại API live hoặc sửa transport/đăng nhập.
- Standalone CLI và demo tuần tự offline chạy được; output DRAFT_UNVERIFIED, stop_reason đúng
  single_pass_completed, demo có một retrieval và một Judge evaluation.
- Gemini live path được test bằng SDK/HTTP transport giả lập, gồm demo hai provider calls
  với schema Judge rồi schema Summary. **Không có request tới API live** và chưa kiểm chứng model/account thật.
- Node regression tùy chọn của ChatGPT provider cũ: **20/21 đạt**, test Windows DPAPI lỗi
  `CHATGPT_SECURE_STORAGE_ERROR`. Chạy riêng cùng test trên checkout ổ D chưa sửa cũng lỗi như vậy;
  đây là hạn chế storage/helper trong môi trường chạy hiện tại, không sửa hay bỏ qua test đó.
- `git diff --check` đạt; BasicRAG/Judge contracts và implementation không sửa.
- Chạy Summary độc lập từ JSON đã lưu cũng đạt trên Python **3.12.14**, không cài Gemini SDK;
  kiểm tra 4 schemas JSON đã xuất khớp contract trong code. Demo index vẫn cần runtime tương thích index.

Không có metric chất lượng pháp lý từ mock. SDK tests sẽ skip nếu chưa cài optional dependency.
Kết quả Python/log chạy được lưu local trong `outputs/summary-python-tests.txt`; outputs là artifact
thử nghiệm được gitignore, không phải benchmark chất lượng nội dung.

## 17. Đánh giá Summary bằng human review

### Thử policy trả lời từng phần (cập nhật 02/11/2026)

Policy mới là mặc định, không cần flag context-policy. CLI summarize/demo và API summarize() giữ cách gọi.
Từ root project hiện tại:

```powershell
Set-Location -LiteralPath 'D:\Trang\TilLegal (Legal AI)\tillegal-rag-judge-summary'
$py = '.\.tmp\python314\python.exe'
$review = 'outputs/partial-review-' + [guid]::NewGuid().ToString('N')
& $py -B -m examples.summary.partial_review --output-dir $review
& $py -B -m ai_core.summary_cli summarize --input "$review/partial-condition.input.json" --mock-response "$review/model.fixture.json" --output "$review/result.json" --telemetry "$review/telemetry.json"
$result = Get-Content -LiteralPath "$review/result.json" -Raw -Encoding UTF8 | ConvertFrom-Json
$result.display_response
$result.issue_reports | ConvertTo-Json -Depth 8
```

Nguồn giả định của generator nêu quy tắc B và ngoại lệ C, không phải pháp luật. Fixture Judge cho I1 partial,
I2 full. Group I1 có neighbor không được Judge chọn; I2 chỉ dùng nguồn độc lập. Output plumbing có thể là:

```text
Đoạn nguồn giả định nêu điều kiện B cho việc tổ chức đối thoại với nhóm A. [R1]
Đoạn ngoại lệ giả định nêu không áp dụng quy tắc mẫu khi có điều kiện C. [R2]

Bản nháp chưa được kiểm chứng ...
Giới hạn - I1: Quy tắc mẫu và khả năng áp dụng
Mới trả lời được một phần ...
- Chưa đủ dữ kiện B/C để kết luận áp dụng.
- Một số đoạn cùng nhóm chưa được chọn làm căn cứ; chỉ sử dụng những đoạn được cấp.
Giới hạn - I2: Nội dung đoạn ngoại lệ giả định
Đã trình bày các ý có căn cứ; chưa coi đây là câu trả lời bao phủ toàn bộ vấn đề.
```

Đây là response fixture có chủ đích, không phải model đã chứng minh giữ đúng điều kiện/ngoại lệ.
`review_cases.json` có ba câu hỏi và checklist human review: partial-condition, missing-exception,
no-evidence-is-not-no-rule. Muốn xem nội dung model sinh thực tế, chạy riêng từng input bằng live opt-in;
không dùng fixture response rồi gọi đó là đánh giá model.

Ví dụ chạy lại **chỉ Summary** từ input thật của một lần Search/Judge đã lưu, không tốn thêm lượt Judge:

```powershell
$savedRun = 'D:\Trang\TilLegal (Legal AI)\summarize-f4db513c06114e2f9df9458b41cd4a9c'
$id = [guid]::NewGuid().ToString('N')
& $py -B -m ai_core.summary_cli summarize --input "$savedRun/input.json" --mode live --provider chatgpt --allow-live --timeout 120 --output "$savedRun/partial-summary-$id.json" --telemetry "$savedRun/partial-summary-$id-telemetry.json"
(Get-Content -LiteralPath "$savedRun/partial-summary-$id.json" -Raw -Encoding UTF8 | ConvertFrom-Json).display_response
```

Nếu dùng run khác, thay savedRun bằng đúng thư mục có input.json. Không sửa bundle/Judge để ép đủ.
Live vẫn có thể abstain nếu không viết được ý độc lập có căn cứ, hoặc trả ERROR nếu scope/schema/provider lỗi.
Model thiếu statement_type=source_statement cho unit có rủi ro sẽ bị từ chối, không tự gán để hợp thức hóa.

Kiểm tra ngày 02/11/2026 trong bản sửa policy: **142 tests Python đạt**, bao gồm **53 tests Summary**,
không skip trong runtime Python 3.14.2/Unicode 16 với Gemini SDK hiện có. Trong đó 13 test policy mới
kiểm tra partial/full issues độc lập, phân biệt 3 kinds, scope không biết, ID ngoài usable, scope guard,
unscoped missing_items, abstention hợp lệ, public report/display integrity và nội dung system prompt.
BasicRAG/Judge regression nằm trong suite; không sửa các module đó. Không gọi API live trong lần sửa này.

Đánh giá tác động phải so sánh policy cũ/mới trên cùng input/evidence/Judge/model/budget: tỷ lệ có câu trả lời,
tỷ lệ abstain, số claim thực sự supported, lỗi bỏ điều kiện/ngoại lệ, kết luận áp dụng khi thiếu context,
claim coverage theo issue và latency/usage. Nguồn/claim giả định chỉ giúp reviewer luyện checklist;
không là bộ gold cho pháp luật Việt Nam. Chưa có phép đo human-labelled nên **không khẳng định độ chính xác
giữ nguyên hoặc tăng** khi tăng tỷ lệ trả lời; giới hạn này là rủi ro thực của thay đổi policy.

Đóng băng câu hỏi/issues, corpus snapshot, evidence được chọn, Judge result, model/prompt version và
ngân sách. Reviewer đọc toàn bộ nguồn cần thiết, không chỉ nhìn JSON hoặc retrieval score.

| Tiêu chí | Cách kiểm tra |
| --- | --- |
| Claim coverage | Các ý cần trả lời nào đã có căn cứ và được draft phản ánh? Phần thiếu đã nêu rõ chưa? |
| Citation mapping errors | Mỗi claim có dẫn đúng đoạn/nguồn không? Tính riêng ID lỗi và nội dung không được nguồn hỗ trợ |
| Điều kiện/ngoại lệ | Có bỏ phủ định, phạm vi, ngoại lệ hoặc biến quy tắc có điều kiện thành tuyệt đối không? |
| Tuân thủ evidence | Có thêm quy định/kết luận ngoài nguồn, tự dựng URL hoặc khẳng định hiệu lực chưa chứng minh không? |
| Abstention | Thiếu căn cứ có từ chối phù hợp không? Context policy có từ chối quá nhiều ca có thể trả lời không? |
| Latency/usage | Đo Summary và từng provider riêng, p50/p95 trên tập cố định; ghi tokens thực nếu provider trả |

Chỉ công bố điểm sau khi có gold/rubric và người đánh giá độc lập; định nghĩa rõ mẫu số và cách xử lý lỗi.
Structural correctness là schema/ID/render/provenance copy đúng; semantic faithfulness là câu trả lời
được nội dung nguồn hỗ trợ. Hai việc cần báo cáo riêng. Tỷ lệ JSON hợp lệ không thay cho citation F1.
Chi phí tiền cần bảng giá/model thực tế, không suy ra từ latency hoặc fixture usage.

## 18. Chưa triển khai và cách nối tiếp

Chưa có LangGraph/state machine, loop, Query Agent mới, Verify, Faith-Search, HTTP backend/frontend/DB,
fine-tuning, nguồn web mới hoặc vector database. Không thay đổi BasicRAG/Judge public contracts.

LangGraph sau này giữ budgets/history và lựa chọn nhánh bên ngoài, tạo input Summary rồi gọi
`summarize()`. Future Verify nhận request/version/draft/claims/citations cùng nguồn truy nguyên được,
kiểm chứng nội dung/vị trí/thời điểm, lưu report gắn version. Nếu cần sửa, caller chuyển report
sang revision handoff ở mục 11, tăng version và audit lại output mới. Summary không tự chuyển nhãn
verified hoặc reuse report cũ cho draft đã đổi.
