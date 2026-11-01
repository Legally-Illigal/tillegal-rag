
# Tillegal — Vietnamese Legal RAG Assistant

## Judge Agent handoff

Nhánh này bàn giao module **Judge đánh giá độ đầy đủ của evidence trước generation**, kèm baseline BasicRAG cần để chạy độc lập. Đây là nhánh riêng từ `main`; không thay đổi nhánh `feature/rag-langgraph` của repository.

- Luồng: retrieval → EvidenceBuilder → adapter → Judge → Summary của caller.
- Provider: mock (mặc định), Anthropic API, OpenAI API, Gemini API; luồng ChatGPT cũ được giữ riêng. Gemini/OpenAI API không cần Node hoặc đăng nhập ChatGPT.
- Python API và CLI: `ai_core/judge_agent.py`, `ai_core/judge_cli.py`; ví dụ backend trong `examples/judge/backend_gemini.py` và `backend_openai.py`.
- Báo cáo: [Judge tổng quan](docs/judge_agent.md), [Gemini API](docs/judge_gemini_api.md), [OpenAI API](docs/judge_openai_api.md).

Chạy mock từ repo root (không cần key hoặc SDK):

```powershell
python -B -m ai_core.judge_cli evaluate --input examples/judge/input.json --provider gemini --mock-response examples/judge/sufficient.json
```

Đây là replay fixture, không phải suy luận ngữ nghĩa. Với Gemini live, cài `requirements-gemini.txt`, đặt `GEMINI_API_KEY`/`GEMINI_MODEL` trên server và chủ động bật cả `--mode live --allow-live`. Xem báo cáo để nhập key an toàn, chạy demo BM25 và lấy telemetry. Không có key thật hoặc phiên đăng nhập trong repository.

Kiểm thử offline: `python -B -m unittest discover -s tests -v`; SDK tests cần dependency Gemini. Smoke index đã kiểm tra trên Python 3.14 (Unicode 16); giữ kiểm tra integrity nếu dùng runtime khác. Vòng lặp LangGraph chưa được triển khai trong module Judge này.

Summarize Agent độc lập và demo một lượt `retrieval -> EvidenceBuilder -> Judge -> Summary` được mô tả trong [báo cáo Summary](docs/summary_agent.md). Chạy từ JSON đã lưu bằng `python -B -m ai_core.summary_cli summarize`; fixture offline phải được chỉ định, live Gemini hoặc ChatGPT cần opt-in. Output gồm draft, claims, citations, limitations và version, luôn chưa Verify. BasicRAG giữ nguyên contract baseline.

Tillegal is a Retrieval-Augmented Generation (RAG) system for Vietnamese legal question answering. The system retrieves relevant legal provisions from an indexed document collection and uses a Large Language Model (LLM) to generate evidence-grounded answers with source citations.

The current release provides a sequential RAG pipeline with hybrid retrieval, reranking, evidence construction, and LLM-based answer generation.

> **Project status:** RAG baseline (experimental). Generated answers and citations have not undergone independent legal verification and must not be treated as authoritative legal advice.

## 1. Features

- **BM25 Retrieval:** Keyword-based retrieval over indexed Vietnamese legal documents.
- **Dense Retrieval:** Semantic search using multilingual text embeddings.
- **Hybrid Retrieval:** Combines BM25 and dense retrieval using Reciprocal Rank Fusion (RRF).
- **Reranking:** Reorders retrieved candidates using a cross-encoder model.
- **Evidence Construction:** Builds a structured context from retrieved legal passages.
- **LLM Generation:** Generates answers grounded in retrieved evidence.
- **Candidate Citations:** Returns document references, provision metadata, and supporting excerpts.
- **Execution Traces:** Records retrieval and generation information for debugging and evaluation.
- **Interactive CLI:** Supports offline mock execution and live LLM inference.
- **Python Integration:** Exposes `BasicRAG.answer()` for integration with external services.

## 2. System Architecture

```text
                       User Question
                             |
                             v
                      Query Processing
                             |
               +-------------+-------------+
               |                           |
               v                           v
         BM25 Retrieval              Dense Retrieval
               |                           |
               +-------------+-------------+
                             |
                             v
                   Hybrid Fusion (RRF)
                             |
                             v
                         Reranker
                             |
                             v
                      Evidence Builder
                             |
                             v
                       LLM Generation
                             |
                             v
                  Answer + Candidate Citations
```

The retrieval strategy is configurable. BM25, dense retrieval, and hybrid retrieval can be executed independently. Reranking is optional.

The current pipeline is sequential. Multi-agent orchestration and automated legal citation verification are not included in this release.

## 3. Technology Stack

| Component | Technology |
| --- | --- |
| Language | Python |
| Keyword Retrieval | BM25 |
| Semantic Retrieval | Dense Embeddings |
| Retrieval Fusion | Reciprocal Rank Fusion |
| Embedding Model | `intfloat/multilingual-e5-small` |
| Reranker Model | `BAAI/bge-reranker-v2-m3` |
| LLM Generation | Anthropic Claude / compatible chat-completions provider |
| Model Framework | PyTorch, Transformers |
| Data Processing | PyArrow, Beautiful Soup |
| Integration Interface | Python API |

## 4. Installation

### 4.1. Requirements

The reference environment is:

- Windows
- Python 3.14.2
- Internet access for initial model downloads
- Sufficient disk space for model artifacts

The dependency lock file was generated for the reference environment. Compatibility with other Python versions and operating systems has not yet been fully validated.

### 4.2. Clone the Repository

```powershell
git clone https://github.com/lynkvu2901/Tilligal.git
cd Tilligal
```

### 4.3. Create a Virtual Environment

```powershell
python -m venv .venv
```

Install the project dependencies:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
```

Verify the installation:

```powershell
.\.venv\Scripts\python.exe -m pip check
```

All subsequent commands assume execution from the repository root.

## 5. Data and Retrieval Indexes

The repository includes the retrieval indexes required for the current pilot corpus.

### 5.1. BM25 Index

```text
data/indexes/labor-pilot-3-t4-bm25/
├── index.json
└── manifest.json
```

The BM25 index stores the document chunks and metadata required for retrieval.

The current pilot contains **3,085 chunks** derived from three legal documents.

### 5.2. Dense Index

```text
data/indexes/labor-pilot-3-t7-dense/
├── manifest.json
├── rows.json
└── vectors.npy
```

The dense index contains vector representations associated with the indexed legal passages.

### 5.3. Dataset Requirements

Raw Parquet files are not required for inference when using the existing retrieval indexes.

Raw data is only necessary when rebuilding indexes, changing document processing rules, or extending the legal corpus.

**Important:** The current indexes cover a limited pilot corpus. The system is not designed to provide comprehensive coverage of Vietnamese legislation in its present state.

## 6. Model Setup

Model weights are not included in the repository and must be downloaded before using dense retrieval and reranking.

### 6.1. Download the Embedding Model

Model: `intfloat/multilingual-e5-small`

```powershell
.\.venv\Scripts\python.exe -B -m scripts.setup_t7_models --kind embedding --revision 614241f622f53c4eeff9890bdc4f31cfecc418b3
```

### 6.2. Download the Reranker

Model: `BAAI/bge-reranker-v2-m3`

```powershell
.\.venv\Scripts\python.exe -B -m scripts.setup_t7_models --kind reranker --revision 953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e
```

The setup scripts create model configuration and cache artifacts used by the retrieval runtime.

### 6.3. Compatibility and Integrity

The runtime validates relationships between:

- Retrieval indexes
- Source code
- Model revisions
- Model configurations
- Artifact manifests

Index manifests include integrity information. Changes to source files, line endings, model versions, or artifact locations may cause validation failures.

Do not disable integrity checks or manually alter stored hashes to bypass these errors.

The existing dense index was prepared for a specific model snapshot. Rebuilding may be necessary if the execution environment or model configuration is incompatible.

## 7. Running the Application

Tillegal provides an interactive command-line interface through:

```text
ai_core.demo_cli
```

Two execution modes are available:

| Mode | Description |
| --- | --- |
| MOCK | Executes the RAG pipeline without calling a paid LLM |
| LIVE | Calls the configured Claude API to generate real answers |

### 7.1. Offline Demo — MOCK

To run hybrid retrieval with reranking:

```powershell
$out = "data/demos/mock-$([guid]::NewGuid().ToString('N'))"

.\.venv\Scripts\python.exe -B -m ai_core.demo_cli --retrieval hybrid --rerank --output-dir $out
```

After initialization, enter a Vietnamese legal question.

Example:

```text
Người sử dụng lao động cần thực hiện những việc gì để tổ chức đối thoại định kỳ tại nơi làm việc?
```

Type `exit` to terminate the session.

**MOCK mode does not generate answers with Claude.** It is intended to validate retrieval, evidence construction, and the execution pipeline.

### 7.2. Live Demo — Claude

LIVE mode requires a valid Anthropic API key.

Set the key in the process environment:

```powershell
$env:ANTHROPIC_API_KEY = "YOUR_ANTHROPIC_API_KEY"
```

Replace the placeholder with an actual API key. Do not commit API keys or environment files containing secrets to Git.

Start the live RAG pipeline:

```powershell
$out = "data/demos/live-$([guid]::NewGuid().ToString('N'))"

.\.venv\Scripts\python.exe -B -m ai_core.demo_cli --retrieval hybrid --rerank --live --output-dir $out
```

The `--live` flag enables real LLM inference.

The application will:

1. Accept a question.
2. Retrieve relevant legal document chunks.
3. Combine keyword and semantic retrieval results.
4. Rerank retrieved candidates.
5. Construct the evidence context.
6. Send the question and evidence to Claude.
7. Return a generated answer with candidate citations.

**LIVE execution incurs API usage costs.**

The CLI uses its own configured Claude model preset. Check `ai_core/demo_cli.py` for the active model configuration.

### 7.3. Output Directories

The CLI writes execution artifacts to the configured output directory.

Use a new output directory for each run to avoid conflicts with existing execution artifacts.

The examples above generate unique directory names automatically.

## 8. Python API

The primary integration entry point is:

```python
ai_core.basic_rag.BasicRAG.answer()
```

The method accepts a question and returns a JSON-serializable Python dictionary.

### 8.1. Basic Example

The following example runs BM25 retrieval with the offline mock provider:

```python
from pathlib import Path
from uuid import uuid4

from ai_core.basic_rag import BasicRAG
from ai_core.bm25_retrieval import load_index
from ai_core.evidence_builder import EvidenceBudget
from ai_core.rag_generation import MockProvider
from ai_core.retrieval_runtime import make_retriever

index = load_index(
    Path("data/indexes/labor-pilot-3-t4-bm25/manifest.json")
)

retriever = make_retriever(
    index,
    mode="bm25",
)

rag = BasicRAG(
    index,
    MockProvider(),
    EvidenceBudget(24, 8000),
    retriever=retriever,
)

result = rag.answer(
    "Đối thoại tại nơi làm việc là gì?",
    request_id=str(uuid4()),
    top_k=3,
)

print(result["status"])
print(result["answer"])
print(result["candidate_citations"])
```

This example uses `MockProvider` and does not make a live LLM request.

For live generation, initialize `BasicRAG` with the configured live provider instead of `MockProvider`.

Refer to the existing implementation in:

- `ai_core/anthropic_provider.py`
- `ai_core/rag_generation.py`
- `ai_core/retrieval_runtime.py`
- `ai_core/demo_cli.py`

The CLI `--live` flag is not a parameter of `BasicRAG.answer()`. Live inference is enabled by selecting an appropriate provider during initialization.

## 9. API Contract

### 9.1. Inputs

| Parameter | Type | Description |
| --- | --- | --- |
| `question` | `str` | User question, maximum 10,000 characters |
| `request_id` | `str` | Non-empty request identifier |
| `top_k` | `int` | Number of retrieved results, from 1 to 100 |

### 9.2. Response Structure

The response is a JSON-serializable dictionary containing fields such as:

| Field | Description |
| --- | --- |
| `schema_version` | Response schema version |
| `request_id` | Request identifier |
| `question` | Original user question |
| `status` | Execution status |
| `answer` | Generated answer or message |
| `candidate_citations` | Candidate document citations |
| `evidence_bundle` | Evidence context |
| `verification_status` | Citation verification status |
| `warnings` | Execution and data-quality warnings |
| `trace` | Retrieval and generation diagnostics |
| `error` | Error information, if applicable |

Possible statuses:

```text
DRAFT_UNVERIFIED
ABSTAINED_UNVERIFIED
NO_EVIDENCE
FAILED
```

A `DRAFT_UNVERIFIED` result indicates that an answer was generated without completed legal citation verification.

### 9.3. Example Response

The following is a simplified illustrative response, not an actual system output:

```json
{
  "request_id": "example-request-001",
  "status": "DRAFT_UNVERIFIED",
  "answer": "Generated answer based on retrieved legal evidence.",
  "candidate_citations": [],
  "verification_status": "NOT_PERFORMED",
  "warnings": []
}
```

Candidate citation objects may include document identification, legal provision metadata, supporting excerpts, and warnings.

## 10. Backend Integration

Tillegal AI Core is implemented as a Python library.

It does not currently provide a standalone HTTP server.

External backend systems can integrate the AI Core by exposing `BasicRAG.answer()` through an HTTP API or another suitable Python interoperability layer.

Example integration architecture:

```text
Client Application
        |
        v
Application Backend
        |
        | HTTP / RPC
        v
Python AI Service
        |
        v
BasicRAG.answer()
        |
        v
RAG Pipeline
        |
        v
Answer + Candidate Citations
```

### Integration Recommendations

- Initialize retrieval indexes and ML models once during service startup.
- Reuse initialized model instances across requests where safe.
- Assign a unique `request_id` to each request.
- Validate incoming questions and retrieval parameters.
- Handle initialization failures and unsuccessful RAG statuses.
- Apply appropriate request timeouts and concurrency limits.
- Keep LLM API credentials on the server.
- Avoid exposing full traces, raw prompts, or internal diagnostics to end users.
- Treat candidate citations as unverified until an independent verification step has been completed.

A backend-facing HTTP endpoint can be implemented separately. No specific HTTP route is provided by the current AI Core package.

## 11. LLM Providers

### 11.1. Anthropic Claude

The existing AI Core includes an Anthropic provider.

Relevant environment variables:

```text
ANTHROPIC_API_KEY
ANTHROPIC_MODEL
```

For Python integration, the provider implementation supports initialization from the environment with explicit permission for live requests.

Refer to:

```text
ai_core/anthropic_provider.py
```

### 11.2. Compatible Chat-Completions Providers

The project also includes a chat-completions provider abstraction.

Relevant configuration variables:

```text
TILLEGAL_LLM_ENDPOINT
TILLEGAL_LLM_MODEL
TILLEGAL_LLM_API_KEY
```

Other LLM services, including OpenAI-compatible endpoints, may be integrated when their request and response formats are supported.

Compatibility with every GPT model or API version has not been independently verified.

Changing an API key alone does not guarantee provider compatibility.

## 12. Project Structure

```text
Tillegal/
├── ai_core/
│   ├── adapters/
│   ├── anthropic_provider.py
│   ├── basic_rag.py
│   ├── bm25_retrieval.py
│   ├── data_contracts.py
│   ├── demo_cli.py
│   ├── dense_retrieval.py
│   ├── evidence_builder.py
│   ├── hybrid_retrieval.py
│   ├── legal_parser.py
│   ├── rag_cli.py
│   ├── rag_generation.py
│   ├── reranking.py
│   ├── retrieval_contracts.py
│   └── retrieval_runtime.py
├── data/
│   └── indexes/
│       ├── labor-pilot-3-t4-bm25/
│       └── labor-pilot-3-t7-dense/
├── scripts/
│   ├── build_dense_index.py
│   ├── inspect_local_legal.py
│   └── setup_t7_models.py
├── requirements.txt
├── requirements-lock.txt
└── README.md
```

Additional local evaluation data, model caches, and experiment outputs are not required to be committed to the repository.

## 13. Current Limitations

The current release is an experimental RAG baseline with the following limitations:

1. **Limited Corpus:** The indexed dataset contains only three pilot legal documents.
2. **Unverified Citations:** Citation candidates have not undergone independent claim-support verification.
3. **Legal Validity:** The system does not automatically determine whether all referenced provisions remain legally effective.
4. **Synchronous Inference:** The current Python interface is synchronous.
5. **No HTTP Server:** An application integration layer must be implemented separately.
6. **No Multi-Agent Orchestration:** Query, Search, Judge, Summary, and Verify agents are planned for future development.
7. **Deployment Validation:** Linux/macOS deployment, concurrent workloads, and production-scale performance have not been fully validated.

## 14. Roadmap

Planned development includes:

- Query and Search Agents
- Evidence sufficiency assessment
- Multi-agent orchestration
- Claim-level citation verification
- Legal provision and document-version validation
- RAG evaluation and benchmarking
- Improved reliability and observability

These components are not part of the current RAG baseline.

## 15. License and Data Notice

Third-party datasets and models are subject to their respective licenses and usage conditions.

The provenance and redistribution permissions of the local legal corpus and derived artifacts should be verified before public distribution.

This software is an experimental research and development project. Its outputs should not be used as a substitute for verification against authoritative legal sources.
