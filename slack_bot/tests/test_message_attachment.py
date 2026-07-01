# message_attachment CRUD 함수 단위 테스트
import importlib.util
import os
import sys
from unittest.mock import MagicMock

# DB 모델 stub 등록 (SQLAlchemy 없이 테스트)
_models_stub = MagicMock()
_MessageAttachment_own = MagicMock()
_models_stub.MessageAttachment = _MessageAttachment_own
sys.modules.setdefault("db.models", _models_stub)

# _MessageAttachment는 _repo 로드 후 _repo.MessageAttachment로 결정한다.

_config_stub = MagicMock()
_config_stub.DEBUG = False
sys.modules.setdefault("config", _config_stub)

# utils.attachment_result는 외부 의존성 없는 순수 dataclass — stub 불필요
import pytest
from utils.attachment_result import AttachmentResult


def _load_repository():
    """db.repository를 sys.modules["db"] 오염과 무관하게 파일에서 직접 로드한다."""
    repo_path = os.path.join(os.path.dirname(__file__), "..", "db", "repository.py")
    spec = importlib.util.spec_from_file_location("_test_repository", repo_path)
    mod = importlib.util.module_from_spec(spec)
    # db.models 스텁이 sys.modules에 있으므로 exec 시 자동으로 사용된다.
    sys.modules.setdefault("_test_repository", mod)
    spec.loader.exec_module(mod)
    return mod


_repo = _load_repository()
save_attachments = _repo.save_attachments
get_attachments_for_message = _repo.get_attachments_for_message


def _make_att(file_id="F001", name="test.png", mime="image/png", ftype="image", text="분석결과"):
    return AttachmentResult(
        slack_file_id=file_id,
        file_name=name,
        mime_type=mime,
        file_type=ftype,
        analysis_text=text,
    )


class TestSaveAttachments:
    def _make_session(self):
        s = MagicMock()
        s.add = MagicMock()
        s.flush = MagicMock()
        return s

    def test_empty_list_does_nothing(self):
        session = self._make_session()
        save_attachments(session, 1, [])
        session.add.assert_not_called()
        session.flush.assert_called_once()

    def test_single_attachment_adds_once(self):
        session = self._make_session()
        att = _make_att()
        save_attachments(session, 42, [att])
        session.add.assert_called_once()
        session.flush.assert_called_once()

    def test_empty_string_fields_stored_as_none(self):
        session = self._make_session()
        att = AttachmentResult(slack_file_id="", file_name="", mime_type="", file_type="other", analysis_text="")
        MA = _repo.MessageAttachment
        MA.reset_mock()
        save_attachments(session, 99, [att])
        _, kwargs = MA.call_args
        assert kwargs["slack_file_id"] is None
        assert kwargs["file_name"] is None
        assert kwargs["mime_type"] is None
        assert kwargs["analysis_text"] is None

    def test_multiple_attachments(self):
        session = self._make_session()
        atts = [_make_att(f"F{i:03d}") for i in range(3)]
        save_attachments(session, 7, atts)
        assert session.add.call_count == 3
        session.flush.assert_called_once()


class TestGetAttachmentsForMessage:
    def test_returns_ordered_query_result(self):
        session = MagicMock()
        mock_rows = [MagicMock(), MagicMock()]
        (session.query.return_value
         .filter.return_value
         .order_by.return_value
         .all.return_value) = mock_rows

        result = get_attachments_for_message(session, 5)

        assert result == mock_rows
        # _repo.MessageAttachment는 repository.py가 실제로 사용하는 클래스 참조
        session.query.assert_called_once_with(_repo.MessageAttachment)
