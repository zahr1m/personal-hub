from hub.pii import mask_pii


def test_mask_card_and_passport():
    assert (
        mask_pii("карта 4276 1234 5678 9012, паспорт 03 15 123456")
        == "карта [номер скрыт], паспорт [номер скрыт]"
    )


def test_mask_card_plain_and_dashes():
    assert mask_pii("4276123456789012 и 4276-1234-5678-9012") == "[номер скрыт] и [номер скрыт]"


def test_mask_keeps_dates_money_phone():
    s = "до 14.03.2027 оплатить 8 400 ₽, звонить +7 918 123-45-67"
    assert mask_pii(s) == s


def test_mask_card_glued_to_letters_and_underscores():
    for s in ("карта4276123456789012", "4276123456789012р", "4276_1234_5678_9012"):
        assert "4276" not in mask_pii(s)
        assert "[номер скрыт]" in mask_pii(s)


def test_mask_passport_variants():
    for s in (
        "03 15 123456",
        "0315 123456",
        "паспорт 0315123456",
        "паспорт0315123456",
        "серия и номер: 0315123456",
    ):
        assert mask_pii(s).endswith("[номер скрыт]"), s
        assert "123456" not in mask_pii(s), s


def test_bare_ten_digits_without_passport_word_kept():
    for s in ("9181234567", "ИНН 2310123456"):
        assert mask_pii(s) == s
