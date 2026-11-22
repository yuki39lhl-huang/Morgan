import asyncio
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

import aiohttp

from account_position_stream import AccountPositionStream, FLAT_MAX_AGE, snapshot_is_flat


class PositionStreamTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = Mock(return_value=10.0)
        self.fetch = Mock(return_value=[{"positionAmt": "0"}])
        self.stream = AccountPositionStream("wss://example.com/ws", None, self.fetch, clock=self.clock)

    def test_snapshot_includes_dust_and_rejects_invalid_responses(self):
        self.assertIs(snapshot_is_flat([]), True)
        self.assertIs(snapshot_is_flat([{"positionAmt": "0"}]), True)
        self.assertIs(snapshot_is_flat([{"positionAmt": "-0.000001"}]), False)
        self.assertIsNone(snapshot_is_flat({"code": -1003}))
        self.assertIsNone(snapshot_is_flat([{"positionAmt": "nan"}]))
        self.assertIsNone(snapshot_is_flat([{}]))

    async def test_confirms_flat_only_online_and_with_fresh_snapshot(self):
        await self.stream._refresh()
        self.assertFalse(self.stream.confirms_flat())
        self.stream.connected = True
        await self.stream._refresh()
        self.assertTrue(self.stream.confirms_flat())
        self.clock.return_value += FLAT_MAX_AGE
        self.assertFalse(self.stream.confirms_flat())
        await self.stream._refresh()
        self.assertTrue(self.stream.confirms_flat())
        self.stream.handle_event({"e": "ACCOUNT_UPDATE", "a": {"P": [{"pa": "-0.1"}]}})
        self.assertFalse(self.stream.confirms_flat())
        self.assertGreater(self.stream._refresh_not_before, self.clock())
        self.fetch.return_value = [{"positionAmt": "-0.1"}]
        await self.stream._refresh()
        self.assertFalse(self.stream.confirms_flat())

    async def test_fill_invalidates_snapshot_in_flight(self):
        started = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()

        def fetch():
            loop.call_soon_threadsafe(started.set)
            release.wait(timeout=2)
            return [{"positionAmt": "0"}]

        self.stream.connected = True
        self.stream.fetch_risks = fetch
        refresh = asyncio.create_task(self.stream._refresh())
        await asyncio.wait_for(started.wait(), timeout=2)
        self.stream.handle_event({"e": "ORDER_TRADE_UPDATE", "o": {"x": "TRADE"}})
        release.set()
        await refresh
        self.assertFalse(self.stream.confirms_flat())

    async def test_disconnect_falls_back_even_after_successful_snapshot(self):
        self.stream.connected = True
        await self.stream._refresh()
        self.assertTrue(self.stream.confirms_flat())
        self.assertFalse(self.stream.handle_event({"e": "listenKeyExpired"}))
        self.assertFalse(self.stream.confirms_flat())
        self.stream.connected = True
        self.assertFalse(self.stream.confirms_flat())
        self.fetch.return_value = {"error": "rate_limited"}
        await self.stream._refresh()
        self.assertFalse(self.stream.confirms_flat())

    async def test_listen_key_expiry_received_over_websocket(self):
        from websockets.asyncio.server import serve

        connected = asyncio.Event()
        expire = asyncio.Event()

        async def serve_events(socket):
            connected.set()
            await expire.wait()
            await socket.send('{"e":"listenKeyExpired"}')

        async with serve(serve_events, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            self.stream.socket_url = f"ws://127.0.0.1:{port}/ws"
            self.stream.clock = asyncio.get_running_loop().time
            self.stream._listen_key = AsyncMock(return_value="synthetic")
            with patch("account_position_stream.CACHE_TTL_SEC", -1):
                task = asyncio.create_task(self.stream._connect(None))
                try:
                    await asyncio.wait_for(connected.wait(), timeout=2)
                    async with asyncio.timeout(2):
                        while not self.stream.confirms_flat():
                            await asyncio.sleep(0.01)
                    expire.set()
                    await asyncio.wait_for(task, timeout=2)
                finally:
                    expire.set()
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        self.assertFalse(self.stream.confirms_flat())

    async def test_keepalive_retries_transient_network_failure_once(self):
        self.stream.connected = True
        with (
            patch("account_position_stream.asyncio.sleep", new_callable=AsyncMock) as sleep,
            patch.object(self.stream, "_listen_key", new_callable=AsyncMock) as renew,
        ):
            async def renew_once(session, method):
                self.assertEqual(method, "PUT")
                if renew.await_count == 1:
                    raise aiohttp.ClientConnectionError("dropped connection")
                self.stream.connected = False

            renew.side_effect = renew_once
            await self.stream._keepalive(None)
        self.assertEqual(renew.await_count, 2)
        self.assertEqual(sleep.await_args_list[-1].args, (15,))

    async def test_keepalive_does_not_retry_a_rejected_lease(self):
        self.stream.connected = True
        self.stream._listen_key = AsyncMock(side_effect=RuntimeError("listenKey rejected"))
        with patch("account_position_stream.asyncio.sleep", new_callable=AsyncMock):
            with self.assertRaises(RuntimeError):
                await self.stream._keepalive(None)
        self.stream._listen_key.assert_awaited_once_with(None, "PUT")

    async def test_listen_key_never_redirected_to_unapproved_host(self):
        session = AsyncMock()
        env = Mock(testnet=True, api_key="test", auto_base="https://testnet.binancefuture.com.example.com")
        with patch("account_position_stream.get_binance_env", return_value=env):
            with self.assertRaises(RuntimeError):
                await self.stream._listen_key(session, "POST")
        session.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
