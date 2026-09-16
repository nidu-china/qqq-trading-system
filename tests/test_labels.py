from qqq_trader.labels import ENTRY_REASON_LABELS, all_labels, entry_reason_label


def test_entry_vwap_pullback_chinese():
    assert entry_reason_label("entry_vwap_pullback") == "VWAP 回踩被拒"
    assert ENTRY_REASON_LABELS["vwap_pullback"] == "VWAP 回踩被拒"


def test_all_labels_includes_entry_reasons():
    payload = all_labels()
    assert "entry_reasons" in payload
    assert payload["entry_reasons"]["entry_squeeze_mid_break"] == "Squeeze 中轨突破"


def test_reject_quote_and_daily_loss_labels():
    from qqq_trader.labels import REJECT_LABELS

    assert REJECT_LABELS["daily_loss"]
    assert REJECT_LABELS["missing_bid_ask"]
    assert REJECT_LABELS["macd_reversal_pending_cancelled"]
