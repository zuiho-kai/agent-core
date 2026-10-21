会议 Leader 先通过 meeting_get_speaker 查看发言者及完整原文，再打断观察到的发言租约。将返回的 lease_token 原样作为 expected_token，避免过期判断误停下一位发言者。宿主停止音频并确认打断后，才能授予下一位发言。此操作保留已发布文字，不取消 Agent 工作。
