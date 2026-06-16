# PII 마스킹 필터 단위 테스트
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from utils.pii_filter import apply_pii_filter, has_pii, mask_email, mask_phone, mask_rrn


class TestEmailMasking:
    def test_masks_standard_email(self):
        result = mask_email("담당자 이메일은 user@example.com 입니다.")
        assert "[EMAIL_REMOVED]" in result
        assert "user@example.com" not in result

    def test_masks_multiple_emails(self):
        result = mask_email("a@b.com 과 c@d.org 모두 마스킹")
        assert result.count("[EMAIL_REMOVED]") == 2

    def test_preserves_non_email(self):
        result = mask_email("안녕하세요, 반갑습니다.")
        assert result == "안녕하세요, 반갑습니다."


class TestPhoneMasking:
    def test_masks_mobile_hyphen(self):
        result = mask_phone("연락처: 010-1234-5678")
        assert "[PHONE_REMOVED]" in result
        assert "010-1234-5678" not in result

    def test_masks_mobile_no_hyphen(self):
        result = mask_phone("01012345678")
        assert "[PHONE_REMOVED]" in result

    def test_masks_seoul_number(self):
        result = mask_phone("02-123-4567")
        assert "[PHONE_REMOVED]" in result


class TestRrnMasking:
    def test_masks_rrn(self):
        result = mask_rrn("주민번호: 900101-1234567")
        assert "[RRN_REMOVED]" in result
        assert "900101-1234567" not in result


class TestApplyPiiFilter:
    def test_combined_pii(self):
        text = "이메일 test@co.kr, 전화 010-9999-0000"
        result = apply_pii_filter(text)
        assert "test@co.kr" not in result
        assert "010-9999-0000" not in result
        assert "[EMAIL_REMOVED]" in result
        assert "[PHONE_REMOVED]" in result

    def test_empty_string(self):
        assert apply_pii_filter("") == ""

    def test_none_passthrough(self):
        assert apply_pii_filter(None) is None

    def test_no_pii_unchanged(self):
        text = "회의실 예약은 어떻게 하나요?"
        assert apply_pii_filter(text) == text


class TestHasPii:
    def test_detects_email(self):
        assert has_pii("user@example.com")

    def test_detects_phone(self):
        assert has_pii("010-1234-5678")

    def test_no_pii_returns_false(self):
        assert not has_pii("일반 텍스트입니다")

    def test_empty_returns_false(self):
        assert not has_pii("")
