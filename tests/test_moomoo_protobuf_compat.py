from __future__ import annotations

import subprocess
import sys
import textwrap


def test_moomoo_stock_screen_parser_accepts_a_protobuf_response() -> None:
    script = textwrap.dedent(
        """\
        import logging.handlers
        import os

        logging.handlers.TimedRotatingFileHandler = (
            lambda *_args, **_kwargs: logging.NullHandler()
        )
        os.makedirs = lambda *_args, **_kwargs: None

        from moomoo.common.pb import Qot_StockScreen_pb2
        from moomoo.quote.quote_query import StockScreenQuery

        response = Qot_StockScreen_pb2.Response()
        response.retType = 0
        result = response.s2c.dataList.add().results.add()
        property_message = result.basicPropertyResult.property
        field = next(
            field
            for field in property_message.DESCRIPTOR.fields
            if field.message_type is None
        )
        if field.type == field.TYPE_STRING:
            value = "US.AAPL"
        elif field.type == field.TYPE_BYTES:
            value = b"US.AAPL"
        elif field.type == field.TYPE_ENUM:
            value = field.enum_type.values[0].number
        elif field.type == field.TYPE_BOOL:
            value = True
        elif field.type in {field.TYPE_FLOAT, field.TYPE_DOUBLE}:
            value = 1.0
        else:
            value = 1
        if field.is_repeated:
            getattr(property_message, field.name).append(value)
            expected = [value]
        else:
            setattr(property_message, field.name, value)
            expected = value

        _status, _message, (_last_page, _count, rows) = StockScreenQuery.unpack(response)
        assert rows[0]["results"][0]["property"][field.name] == expected
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
