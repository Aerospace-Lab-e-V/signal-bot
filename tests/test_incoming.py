from app.incoming import handle_signal_updates, iter_received_messages


def test_iter_received_messages_reads_data_message_envelope():
    payload = [{"envelope": {"dataMessage": {"message": "!ping"}}}]

    messages = list(iter_received_messages(payload))

    assert messages[0]["text"] == "!ping"
    assert messages[0]["raw"] == payload[0]


def test_iter_received_messages_reads_sync_sent_message():
    payload = {"envelope": {"syncMessage": {"sentMessage": {"message": "!status"}}}}

    messages = list(iter_received_messages(payload))

    assert messages[0]["text"] == "!status"


def test_handle_signal_updates_counts_messages(monkeypatch):
    handled = []
    payload = [
        {"envelope": {"dataMessage": {"message": "!ping"}}},
        {"envelope": {"dataMessage": {"message": ""}}},
        {"message": "plain text"},
    ]

    monkeypatch.setattr("app.incoming.handle_received_message", handled.append)

    count = handle_signal_updates(payload)

    assert count == 2
    assert [message["text"] for message in handled] == ["!ping", "plain text"]
