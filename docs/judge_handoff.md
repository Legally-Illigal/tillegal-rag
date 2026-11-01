# Bàn giao Judge Agent lên tillegal-rag

Ngày 01/11/2026, bản bàn giao được tạo trong checkout riêng để không sửa workspace nguồn `Tilligal-judge-agent` hoặc các thay đổi chưa commit ở đó.

- Nhánh: `feature/judge-agent-api-providers`, dựa trên `main` (`b836be5`).
- Nguồn: snapshot code hiện tại trong `Tilligal-judge-agent`, có baseline từ commit `360aeef` và phần Judge/provider/test/docs đang phát triển.
- Không nhập hoặc thay đổi nhánh `feature/rag-langgraph`. Module Judge vẫn độc lập; việc nối orchestration thuộc bước tích hợp sau.
- Bao gồm baseline Python và các index đã có trong Git nguồn để CLI/demo có đủ dependency nội bộ. Byte của các file index và Python baseline được giữ theo snapshot; không rebuild parser/index.
- Không bao gồm `.tmp`, Python runtime, model cache, `.env`, key thật, OAuth session, credential store hoặc thư mục output live. Kiểm tra mẫu secret không phát hiện chuỗi nghi là key/token trong các file bàn giao; đây không phải chứng nhận audit bảo mật đầy đủ.

Đã chạy lại trên chính checkout bàn giao: **89 test Python pass, 0 skip**, gồm 20 test Gemini, và **21 test Node pass**. SDK Gemini2.34.0; Python3.14.2 phù hợp index Unicode16. Test SDK/HTTP/OAuth đều dùng mock hoặc token giả; không gọi generation trả phí và không xác nhận chất lượng suy luận pháp lý.

Các lỗi live người dùng từng cung cấp vẫn được ghi nhận đúng giới hạn: Google không cho người dùng API mới truy cập 2.5 Flash; các lượt 3.8 Flash có HTTP503/high demand hoặc timeout. Chưa có kết quả Judge Gemini live thành công được xác minh trong bản bàn giao này. Không coi việc push Git hoặc test pass là bằng chứng live hoạt động.

Đọc [báo cáo Judge](judge_agent.md), [Gemini API](judge_gemini_api.md), [OpenAI API](judge_openai_api.md) và README để chạy mock/live. Những đường dẫn local trong các báo cáo cũ mô tả môi trường triển khai ban đầu; trên checkout mới, chạy các lệnh từ repo root và đặt Python/runtime phù hợp.
