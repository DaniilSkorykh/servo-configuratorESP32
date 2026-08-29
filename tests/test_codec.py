"""Тесты кодирования и разбора кадров JSON Lines."""

from __future__ import annotations

import json

import pytest

from servo_configurator.protocol import (
    MAX_LINE_BYTES,
    Command,
    LineAssembler,
    Notification,
    ProtocolError,
    Request,
    Response,
    Telemetry,
    decode_message,
    encode_request,
)
from servo_configurator.protocol.messages import DeviceState


class TestEncodeRequest:
    def test_frame_ends_with_newline(self):
        frame = encode_request(Request(id=1, cmd=Command.PING))
        assert frame.endswith(b"\n")
        assert frame.count(b"\n") == 1

    def test_encodes_id_and_command(self):
        frame = encode_request(Request(id=42, cmd=Command.MOVE_TO, args={"pos": 2048}))
        message = json.loads(frame)
        assert message == {"id": 42, "cmd": "move_to", "args": {"pos": 2048}}

    def test_empty_args_are_omitted(self):
        message = json.loads(encode_request(Request(id=1, cmd=Command.PING)))
        assert "args" not in message

    def test_oversized_frame_is_rejected(self):
        # Кадр должен обнаруживаться до записи в порт, а не обрезаться приёмником.
        huge = Request(id=1, cmd=Command.SET_CONFIG, args={"config": {"x": "y" * 600}})
        with pytest.raises(ProtocolError, match="превышает лимит"):
            encode_request(huge)

    def test_full_config_fits_into_frame(self):
        """Полная конфигурация обязана помещаться в лимит кадра."""
        from servo_configurator.protocol import default_config

        request = Request(id=1, cmd=Command.SET_CONFIG, args={"config": default_config()})
        assert len(encode_request(request)) <= MAX_LINE_BYTES


class TestDecodeResponse:
    def test_success(self):
        response = decode_message('{"id":7,"ok":true,"data":{"state":"homing"}}')
        assert isinstance(response, Response)
        assert (response.id, response.ok, response.data) == (7, True, {"state": "homing"})
        assert response.err is None

    def test_failure(self):
        response = decode_message('{"id":7,"ok":false,"err":"E_RANGE","msg":"out of range"}')
        assert isinstance(response, Response)
        assert response.ok is False
        assert response.err == "E_RANGE"
        assert response.msg == "out of range"

    def test_missing_data_becomes_empty_dict(self):
        assert decode_message('{"id":1,"ok":true}').data == {}

    def test_failure_without_error_code_is_rejected(self):
        with pytest.raises(ProtocolError, match="без кода ошибки"):
            decode_message('{"id":1,"ok":false}')

    @pytest.mark.parametrize(
        "line",
        [
            "not json at all",
            "[1,2,3]",
            '{"id":"seven","ok":true}',
            '{"id":1,"ok":"yes"}',
            '{"foo":"bar"}',
        ],
    )
    def test_malformed_messages_raise(self, line):
        with pytest.raises(ProtocolError):
            decode_message(line)


class TestDecodeNotification:
    def test_event(self):
        event = decode_message('{"evt":"boot","data":{"fw":"1.0.0","proto":1}}')
        assert isinstance(event, Notification)
        assert event.evt == "boot"
        assert event.data["proto"] == 1

    def test_event_without_data(self):
        assert decode_message('{"evt":"state"}').data == {}

    def test_event_without_name_is_rejected(self):
        with pytest.raises(ProtocolError):
            decode_message('{"evt":""}')


class TestLineAssembler:
    def test_single_line(self):
        assert LineAssembler().feed(b'{"a":1}\n') == ['{"a":1}']

    def test_several_lines_in_one_chunk(self):
        assert LineAssembler().feed(b"one\ntwo\nthree\n") == ["one", "two", "three"]

    def test_line_split_across_chunks(self):
        assembler = LineAssembler()
        assert assembler.feed(b'{"id":1,') == []
        assert assembler.feed(b'"ok":true}') == []
        assert assembler.feed(b"\n") == ['{"id":1,"ok":true}']

    def test_incomplete_tail_is_kept(self):
        assembler = LineAssembler()
        assert assembler.feed(b"complete\npartial") == ["complete"]
        assert assembler.feed(b"-rest\n") == ["partial-rest"]

    def test_carriage_return_is_stripped(self):
        assert LineAssembler().feed(b"line\r\n") == ["line"]

    def test_empty_lines_are_ignored(self):
        assert LineAssembler().feed(b"\n\ndata\n\n") == ["data"]

    def test_oversized_line_is_dropped(self):
        assembler = LineAssembler()
        assert assembler.feed(b"x" * (MAX_LINE_BYTES + 10) + b"\n") == []
        assert assembler.dropped == 1

    def test_recovers_after_garbage(self):
        """После мусора без разделителя приём восстанавливается на следующем кадре."""
        assembler = LineAssembler()
        assembler.feed(b"\xff" * (MAX_LINE_BYTES + 1))  # переполнение без \n
        assembler.feed(b"tail-of-garbage\n")             # хвост испорченного кадра
        assert assembler.feed(b'{"evt":"boot"}\n') == ['{"evt":"boot"}']

    def test_undecodable_bytes_are_dropped_without_raising(self):
        assembler = LineAssembler()
        assert assembler.feed(b"\xff\xfe\n") == []
        assert assembler.dropped == 1

    def test_reset_clears_partial_data(self):
        assembler = LineAssembler()
        assembler.feed(b"partial")
        assembler.reset()
        assert assembler.feed(b"fresh\n") == ["fresh"]

    def test_byte_by_byte_delivery(self):
        """Приём по одному байту не должен отличаться от приёма целым куском."""
        assembler = LineAssembler()
        payload = b'{"evt":"tlm","data":{"pos":100}}\n'
        collected = [line for byte in payload for line in assembler.feed(bytes([byte]))]
        assert collected == ['{"evt":"tlm","data":{"pos":100}}']


class TestTelemetry:
    def test_full_frame(self):
        telemetry = Telemetry.from_dict({
            "seq": 10, "ts": 1000, "pos": 2048, "spd": -300, "load": 120,
            "volt": 74, "temp": 31, "cur": 45,
            "moving": True, "state": "position", "homed": True, "err": None,
        })
        assert telemetry.pos == 2048
        assert telemetry.spd == -300
        assert telemetry.state is DeviceState.POSITION
        assert telemetry.moving is True
        assert telemetry.err is None

    def test_optional_fields_may_be_absent(self):
        telemetry = Telemetry.from_dict({"pos": 100, "state": "idle"})
        assert (telemetry.volt, telemetry.temp, telemetry.cur) == (None, None, None)

    def test_unknown_state_falls_back_to_fault(self):
        """Неизвестное состояние трактуется как отказ: безопаснее, чем считать idle."""
        assert Telemetry.from_dict({"state": "dancing"}).state is DeviceState.FAULT

    def test_garbage_values_do_not_raise(self):
        telemetry = Telemetry.from_dict({"pos": "nan", "spd": None, "volt": "x"})
        assert telemetry.pos == 0
        assert telemetry.spd == 0
        assert telemetry.volt is None

    def test_unknown_fields_are_ignored(self):
        assert Telemetry.from_dict({"pos": 5, "future_field": 123}).pos == 5


def test_round_trip_through_assembler():
    """Команда, пропущенная через сборщик, разбирается обратно без потерь."""
    frame = encode_request(Request(id=99, cmd=Command.MOTOR_RUN, args={"dir": "cw"}))
    (line,) = LineAssembler().feed(frame)
    assert json.loads(line) == {"id": 99, "cmd": "motor_run", "args": {"dir": "cw"}}
