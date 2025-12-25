"""
WebMKS to noVNC WebSocket Proxy

Bridges noVNC client (browser) ↔ ESXi WebMKS (VNC over WebSocket).
Based on protocol analysis from markpeek/webmks.

Architecture:
  noVNC (browser) → WS → [Proxy] → WS (binary subprotocol) → ESXi WebMKS (VNC/RFB)
"""

import asyncio
import logging
import ssl
import os
from typing import Optional

import aiohttp
from aiohttp import web
import websockets

logger = logging.getLogger(__name__)


class WebMKSProxy:
    """
    WebSocket proxy that bridges noVNC client to ESXi WebMKS.

    The proxy handles two WebSocket connections:
    1. Client-side: noVNC in browser (standard RFB/VNC WebSocket)
    2. Server-side: ESXi WebMKS (requires 'binary' subprotocol)
    """

    def __init__(self, esxi_host: str, verify_ssl: bool = False):
        """
        Initialize proxy.

        Args:
            esxi_host: ESXi hostname or IP address
            verify_ssl: Whether to verify SSL certificates
        """
        self.esxi_host = esxi_host
        self.verify_ssl = verify_ssl
        self.active_connections = {}

    async def handle_client(self, client_ws: web.WebSocketResponse, session_id: str, ticket_data: dict):
        """
        Handle WebSocket connection from noVNC client.

        Args:
            client_ws: WebSocket connection from browser/noVNC
            session_id: Unique session identifier
            ticket_data: Dict with 'ticket', 'host', 'port' from ESXi AcquireTicket
        """
        # Extract ticket information
        ticket = ticket_data['ticket']
        ws_host = ticket_data.get('host') or self.esxi_host
        ws_port = ticket_data.get('port', 443)

        # Construct WebMKS URL
        webmks_url = f"wss://{ws_host}:{ws_port}/ticket/{ticket}"

        logger.info(f"[{session_id}] Connecting to WebMKS: {webmks_url}")

        # SSL context for ESXi connection (usually self-signed cert)
        ssl_context = ssl.create_default_context()
        if not self.verify_ssl:
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

        try:
            # Connect to ESXi WebMKS with CRITICAL 'binary' subprotocol
            async with websockets.connect(
                webmks_url,
                ssl=ssl_context,
                subprotocols=['binary'],  # CRITICAL!
                origin='http://localhost',
                ping_interval=None,  # Disable automatic pings - ESXi handles keepalive
                ping_timeout=None
            ) as esxi_ws:

                logger.info(f"[{session_id}] Connected to ESXi WebMKS")
                self.active_connections[session_id] = {
                    'client': client_ws,
                    'esxi': esxi_ws
                }

                # Bidirectional relay
                await asyncio.gather(
                    self._relay_client_to_esxi(client_ws, esxi_ws, session_id),
                    self._relay_esxi_to_client(esxi_ws, client_ws, session_id),
                    return_exceptions=True
                )

        except Exception as e:
            logger.error(f"[{session_id}] WebMKS connection error: {e}")
            raise

        finally:
            if session_id in self.active_connections:
                del self.active_connections[session_id]
            logger.info(f"[{session_id}] Connection closed")

    async def _relay_client_to_esxi(self, client_ws, esxi_ws, session_id: str):
        """Relay messages from noVNC client to ESXi WebMKS"""
        try:
            async for msg in client_ws:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    # Forward binary data to ESXi
                    await esxi_ws.send(msg.data)
                    logger.debug(f"[{session_id}] Client→ESXi: {len(msg.data)} bytes")

                elif msg.type == aiohttp.WSMsgType.TEXT:
                    # noVNC shouldn't send text, but handle it anyway
                    await esxi_ws.send(msg.data)
                    logger.debug(f"[{session_id}] Client→ESXi: text {len(msg.data)} chars")

                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error(f"[{session_id}] Client WS error: {client_ws.exception()}")
                    break

                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                    logger.info(f"[{session_id}] Client disconnected")
                    break

        except Exception as e:
            logger.error(f"[{session_id}] Error relaying client→ESXi: {e}")
        finally:
            await esxi_ws.close()

    async def _relay_esxi_to_client(self, esxi_ws, client_ws, session_id: str):
        """Relay messages from ESXi WebMKS to noVNC client"""
        try:
            async for msg in esxi_ws:
                if isinstance(msg, bytes):
                    # Forward binary data to client
                    await client_ws.send_bytes(msg)
                    logger.debug(f"[{session_id}] ESXi→Client: {len(msg)} bytes")
                else:
                    # Text message (shouldn't happen with binary subprotocol)
                    await client_ws.send_str(msg)
                    logger.debug(f"[{session_id}] ESXi→Client: text {len(msg)} chars")

        except websockets.exceptions.ConnectionClosed:
            logger.info(f"[{session_id}] ESXi disconnected")
        except Exception as e:
            logger.error(f"[{session_id}] Error relaying ESXi→client: {e}")
        finally:
            await client_ws.close()


class ProxyServer:
    """
    HTTP/WebSocket server for the proxy.

    Endpoints:
        WS /proxy/{session_id} - WebSocket endpoint for noVNC
    """

    def __init__(self, esxi_host: str, verify_ssl: bool = False):
        self.proxy = WebMKSProxy(esxi_host, verify_ssl)
        self.sessions = {}  # session_id → ticket_data

    async def websocket_handler(self, request: web.Request):
        """Handle WebSocket connections from noVNC clients"""
        session_id = request.match_info.get('session_id')

        if not session_id or session_id not in self.sessions:
            logger.warning(f"Invalid session: {session_id}")
            return web.Response(status=400, text="Invalid session ID")

        ticket_data = self.sessions[session_id]

        ws = web.WebSocketResponse()
        await ws.prepare(request)

        logger.info(f"[{session_id}] Client connected")

        try:
            await self.proxy.handle_client(ws, session_id, ticket_data)
        except Exception as e:
            logger.error(f"[{session_id}] Proxy error: {e}")
        finally:
            # Clean up session after use
            if session_id in self.sessions:
                del self.sessions[session_id]

        return ws

    async def create_session(self, request: web.Request):
        """
        Create a new proxy session.

        POST /api/sessions
        Body: {
            "ticket": "...",
            "host": "...",  # optional
            "port": 443     # optional
        }

        Returns: {
            "session_id": "...",
            "ws_url": "/proxy/{session_id}"
        }
        """
        try:
            data = await request.json()
            ticket = data.get('ticket')

            if not ticket:
                return web.json_response({'error': 'Missing ticket'}, status=400)

            # Generate session ID
            import uuid
            session_id = str(uuid.uuid4())

            # Store session
            ticket_data = {
                'ticket': ticket,
                'host': data.get('host'),
                'port': data.get('port', 443)
            }
            self.sessions[session_id] = ticket_data

            logger.info(f"Created session {session_id} - ticket_data: host={ticket_data['host']}, port={ticket_data['port']}")

            return web.json_response({
                'session_id': session_id,
                'ws_url': f'/proxy/{session_id}'
            })

        except Exception as e:
            logger.error(f"Error creating session: {e}")
            return web.json_response({'error': str(e)}, status=500)

    def create_app(self):
        """Create aiohttp application"""
        app = web.Application()
        app.router.add_post('/api/sessions', self.create_session)
        app.router.add_get('/proxy/{session_id}', self.websocket_handler)
        return app


def run_server(host: str = '0.0.0.0', port: int = 8765, esxi_host: str = None, verify_ssl: bool = False):
    """
    Run the WebMKS proxy server.

    Args:
        host: Host to bind to
        port: Port to listen on
        esxi_host: ESXi hostname/IP
        verify_ssl: Whether to verify SSL certificates
    """
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    if not esxi_host:
        esxi_host = os.getenv('ESXI_HOST', 'localhost')

    logger.info(f"Starting WebMKS Proxy Server")
    logger.info(f"  Listening on: {host}:{port}")
    logger.info(f"  ESXi host: {esxi_host}")
    logger.info(f"  SSL verification: {verify_ssl}")

    server = ProxyServer(esxi_host, verify_ssl)
    app = server.create_app()

    web.run_app(app, host=host, port=port)


if __name__ == '__main__':
    from dotenv import load_dotenv
    load_dotenv()

    run_server(
        host=os.getenv('WS_PROXY_HOST', '0.0.0.0'),
        port=int(os.getenv('WS_PROXY_PORT', 8765)),
        esxi_host=os.getenv('ESXI_HOST'),
        verify_ssl=os.getenv('ESXI_VERIFY_SSL', 'false').lower() == 'true'
    )
