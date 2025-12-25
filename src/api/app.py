"""
Flask API for ESXi WebMKS Console Portal

Provides REST API for VM management and console session creation.
"""

import os
import time
import urllib3
from flask import Flask, jsonify, request, send_from_directory, Response
from flask_cors import CORS
from flask_compress import Compress
from dotenv import load_dotenv

# Disable SSL warnings for self-signed ESXi certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Import our utilities
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.utils.esxi_client import ESXiClient
from src.utils.request_queue import get_queue, RequestPriority
from src.utils.esxi_connection_pool import get_connection, get_pool as get_esxi_pool
from src.utils.circuit_breaker import get_breaker, CircuitBreakerOpen
from src.utils.server_manager import get_server_manager
from src.utils.background_refresh import get_refresh_service
from src.services.thumbnail_refresh import ThumbnailRefreshService

load_dotenv()

# Get project root directory
PROJECT_ROOT = Path(__file__).parent.parent.parent
FRONTEND_DIR = PROJECT_ROOT / 'frontend'

app = Flask(__name__, static_folder=str(FRONTEND_DIR))
CORS(app)
Compress(app)  # Enable gzip compression for large responses

# Global thumbnail refresh service instance
_thumbnail_service = None


def get_thumbnail_service():
    """Get or create thumbnail refresh service instance."""
    global _thumbnail_service
    if _thumbnail_service is None:

        def esxi_client_factory(server_config):
            """Factory function to create ESXi clients."""
            return ESXiClient(
                host=server_config['host'],
                user=server_config['user'],
                password=server_config['password'],
                port=server_config['port'],
                verify_ssl=server_config['verify_ssl']
            )

        _thumbnail_service = ThumbnailRefreshService(
            app=app,
            esxi_client_factory=esxi_client_factory,
            thumbnail_cache_ttl=THUMBNAIL_CACHE_TTL
        )
    return _thumbnail_service

# Simple in-memory cache for thumbnails
# Format: {vm_moid: {'data': bytes, 'timestamp': float}}
THUMBNAIL_CACHE = {}
THUMBNAIL_CACHE_TTL = int(os.getenv('THUMBNAIL_CACHE_TTL', 120))  # seconds (increased to 120 for ESXi load)

# Cache for VM list (reduces load for multiple users)
VM_LIST_CACHE = None
VM_LIST_CACHE_TIMESTAMP = 0
VM_LIST_CACHE_TTL = int(os.getenv('VM_LIST_CACHE_TTL', 30))  # seconds

# Configuration
app.config['ESXI_HOST'] = os.getenv('ESXI_HOST')
app.config['ESXI_USER'] = os.getenv('ESXI_USER')
app.config['ESXI_PASSWORD'] = os.getenv('ESXI_PASSWORD')
app.config['ESXI_PORT'] = int(os.getenv('ESXI_PORT', 443))
app.config['ESXI_VERIFY_SSL'] = os.getenv('ESXI_VERIFY_SSL', 'false').lower() == 'true'


@app.route('/')
def index():
    """Serve main page"""
    return send_from_directory(str(FRONTEND_DIR), 'index.html')


@app.route('/<path:path>')
def serve_static(path):
    """Serve static files"""
    return send_from_directory(str(FRONTEND_DIR), path)


@app.route('/api/v1/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({
        'status': 'ok',
        'service': 'esxi-webmks-api'
    })


@app.route('/api/v1/queue/stats', methods=['GET'])
def queue_stats():
    """Get ESXi request queue and circuit breaker statistics"""
    queue = get_queue()
    pool = get_esxi_pool()
    breaker = get_breaker()
    refresh_service = get_refresh_service()
    thumbnail_service = get_thumbnail_service()

    # Count thumbnails in cache (stored in globals with thumbnail_* keys)
    thumbnail_count = sum(1 for key in globals().keys() if key.startswith('thumbnail_') and not key.endswith('_timestamp'))

    return jsonify({
        'queue': queue.get_stats(),
        'pool': pool.get_stats(),
        'circuit_breaker': breaker.get_state(),
        'cache': {
            'thumbnail_count': thumbnail_count,
            'thumbnail_ttl': THUMBNAIL_CACHE_TTL
        },
        'background_refresh': refresh_service.get_stats(),
        'thumbnail_refresh': thumbnail_service.get_stats()
    })


@app.route('/api/v1/background-refresh/invalidate', methods=['POST'])
def invalidate_background_cache():
    """Manually invalidate background cache for a server or all servers"""
    server_id = request.json.get('server_id') if request.json else None
    refresh_service = get_refresh_service()
    refresh_service.invalidate_cache(server_id)

    return jsonify({
        'success': True,
        'message': f'Cache invalidated for {"server " + server_id if server_id else "all servers"}'
    })


@app.route('/api/v1/servers', methods=['GET'])
def list_servers():
    """Get all ESXi servers"""
    try:
        manager = get_server_manager()
        servers = manager.get_all_servers()

        # Don't send passwords to frontend
        safe_servers = []
        for server in servers:
            safe_server = {k: v for k, v in server.items() if k != 'password'}
            safe_server['has_password'] = bool(server.get('password'))
            safe_servers.append(safe_server)

        return jsonify({'servers': safe_servers})
    except Exception as e:
        app.logger.error(f"Error listing servers: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/v1/servers', methods=['POST'])
def add_server():
    """Add new ESXi server"""
    try:
        data = request.json
        manager = get_server_manager()

        server = manager.add_server(
            name=data['name'],
            host=data['host'],
            user=data['user'],
            password=data['password'],
            port=data.get('port', 443),
            verify_ssl=data.get('verify_ssl', False)
        )

        # Start thumbnail refresh for new server
        thumbnail_service = get_thumbnail_service()
        thumbnail_service.start_server_refresh(server['id'], server)
        app.logger.info(f"Started thumbnail refresh for new server {server['name']}")

        # Don't send password back
        safe_server = {k: v for k, v in server.items() if k != 'password'}
        safe_server['has_password'] = True

        return jsonify(safe_server), 201
    except Exception as e:
        app.logger.error(f"Error adding server: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/v1/servers/<server_id>', methods=['PUT'])
def update_server(server_id):
    """Update ESXi server"""
    try:
        data = request.json
        manager = get_server_manager()

        server = manager.update_server(server_id, **data)
        if not server:
            return jsonify({'error': 'Server not found'}), 404

        safe_server = {k: v for k, v in server.items() if k != 'password'}
        safe_server['has_password'] = bool(server.get('password'))

        return jsonify(safe_server)
    except Exception as e:
        app.logger.error(f"Error updating server: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/v1/servers/<server_id>', methods=['DELETE'])
def delete_server(server_id):
    """Delete ESXi server"""
    try:
        manager = get_server_manager()
        success = manager.delete_server(server_id)

        if not success:
            return jsonify({'error': 'Server not found'}), 404

        # Stop thumbnail refresh for deleted server
        thumbnail_service = get_thumbnail_service()
        thumbnail_service.stop_server_refresh(server_id)
        app.logger.info(f"Stopped thumbnail refresh for deleted server {server_id}")

        return jsonify({'success': True})
    except Exception as e:
        app.logger.error(f"Error deleting server: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/v1/servers/<server_id>/test', methods=['POST'])
def test_server(server_id):
    """Test connection to ESXi server"""
    try:
        manager = get_server_manager()
        result = manager.test_connection(server_id)

        return jsonify(result)
    except Exception as e:
        app.logger.error(f"Error testing server: {e}")
        return jsonify({'success': False, 'message': str(e)})


@app.route('/api/v1/servers/<server_id>/stats', methods=['GET'])
def get_server_stats(server_id):
    """
    Get server resource statistics (CPU, Memory, Datastores).

    CACHE-ONLY endpoint - never hits ESXi directly.
    The background refresh service continuously updates stats with VMs every 30 seconds.
    This endpoint only serves from cache to prevent overwhelming ESXi with direct requests.
    """
    manager = get_server_manager()
    server = manager.get_server(server_id)

    if not server:
        return jsonify({'error': 'Server not found'}), 404

    # Get from background refresh cache
    from ..utils.background_refresh import get_refresh_service
    refresh_service = get_refresh_service()
    cached_stats = refresh_service.get_cached_stats(server_id)

    if cached_stats:
        app.logger.debug(f"Stats cache HIT for {server_id} (age: {cached_stats.get('cache_age', 0):.1f}s)")
        # Return stats even if there's an error (stale data + error message)
        return jsonify(cached_stats)

    # No cache available - background service hasn't populated it yet
    app.logger.debug(f"Stats not in cache for {server_id}, background service will populate")
    return jsonify({
        'error': 'Stats not available yet',
        'message': 'Background service is collecting stats, please retry in a few seconds'
    }), 404


@app.route('/api/v1/servers/<server_id>/vms', methods=['GET'])
def list_server_vms(server_id):
    """Get VMs for specific server - uses background cache when available"""
    try:
        manager = get_server_manager()
        server = manager.get_server(server_id)

        if not server:
            return jsonify({'error': 'Server not found'}), 404

        # Try background cache FIRST (always fresh due to auto-refresh)
        refresh_service = get_refresh_service()
        cached_data = refresh_service.get_cached_vms(server_id)

        if cached_data:
            app.logger.debug(f"Background cache HIT for server {server_id} (age: {cached_data['cache_age']:.1f}s)")
            return jsonify(cached_data)

        # Background cache miss (cold start) - fetch directly
        # This only happens on first request after server restart
        app.logger.info(f"Background cache MISS - fetching VMs from server {server_id} ({server['name']})")
        try:
            client = ESXiClient(
                host=server['host'],
                user=server['user'],
                password=server['password'],
                port=server['port'],
                verify_ssl=server['verify_ssl']
            )
            app.logger.info(f"Connecting to {server['host']}...")
            client.connect()
            try:
                app.logger.info(f"Getting VM list...")
                vms = client.get_vms()
                app.logger.info(f"Got {len(vms)} VMs, extracting basic info...")
                # For large VM lists, only return basic info to avoid timeout
                # Full details can be fetched on-demand later
                vm_list = []
                for vm in vms:
                    try:
                        vm_list.append({
                            'name': vm.name,
                            'moid': vm._moId,
                            'power_state': vm.runtime.powerState,
                            'guest_os': vm.config.guestFullName if vm.config else 'Unknown',
                            'memory_mb': vm.config.hardware.memoryMB if vm.config else 0,
                            'num_cpu': vm.config.hardware.numCPU if vm.config else 0,
                            'guest_ip': vm.guest.ipAddress if vm.guest and vm.guest.ipAddress else None
                        })
                    except Exception as e:
                        app.logger.warning(f"Error getting info for VM: {e}")
                        continue
                app.logger.info(f"Completed extracting {len(vm_list)} VMs")
            finally:
                client.disconnect()
        except Exception as e:
            app.logger.error(f"Error fetching VMs from server {server_id}: {e}")

            # Try to return stale background cache if available
            cached_data = refresh_service.get_cached_vms(server_id)
            if cached_data:
                cached_data['stale'] = True
                cached_data['error'] = 'ESXi error - showing cached data'
                return jsonify(cached_data)

            return jsonify({'error': str(e)}), 500

        app.logger.info(f"Direct fetch completed: {len(vm_list)} VMs from {server['name']}")

        return jsonify({
            'vms': vm_list,
            'cached': False
        })

    except Exception as e:
        app.logger.error(f"Error listing VMs for server {server_id}: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/v1/vms', methods=['GET'])
def list_vms():
    """
    Get list of all VMs with caching for multiple users.

    Returns:
        {
            "vms": [
                {
                    "moid": "10",
                    "name": "vm08",
                    "power_state": "poweredOn",
                    "guest_os": "...",
                    "guest_ip": "...",
                    "num_cpu": 2,
                    "memory_mb": 4096
                },
                ...
            ]
        }
    """
    global VM_LIST_CACHE, VM_LIST_CACHE_TIMESTAMP

    try:
        # Check cache first
        current_time = time.time()
        if VM_LIST_CACHE and (current_time - VM_LIST_CACHE_TIMESTAMP) < VM_LIST_CACHE_TTL:
            age = current_time - VM_LIST_CACHE_TIMESTAMP
            app.logger.debug(f"VM list cache HIT (age: {age:.1f}s)")
            return jsonify({
                'vms': VM_LIST_CACHE,
                'cached': True,
                'cache_age': round(age, 1)
            })

        # Cache miss - fetch from ESXi with HIGH priority
        queue = get_queue()
        breaker = get_breaker()

        def fetch_vms():
            with queue.acquire(RequestPriority.HIGH):
                with get_connection() as client:
                    vms = client.get_vms()
                    return [client.get_vm_info(vm) for vm in vms]

        vm_list = breaker.call(fetch_vms)

        # Update cache
        VM_LIST_CACHE = vm_list
        VM_LIST_CACHE_TIMESTAMP = current_time
        app.logger.info(f"VM list cache MISS - cached {len(vm_list)} VMs")

        return jsonify({
            'vms': vm_list,
            'cached': False
        })

    except CircuitBreakerOpen as e:
        app.logger.warning(f"Circuit breaker open for VM list: {e}")
        # Return cached data if available, even if stale
        if VM_LIST_CACHE:
            return jsonify({
                'vms': VM_LIST_CACHE,
                'cached': True,
                'stale': True,
                'error': 'ESXi overloaded - showing cached data'
            })
        return jsonify({'error': str(e)}), 503

    except Exception as e:
        app.logger.error(f"Error listing VMs: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/v1/servers/<server_id>/vms/<moid>/console', methods=['POST'])
def create_server_console_session(server_id, moid):
    """Create console session for VM on specific server"""
    try:
        manager = get_server_manager()
        server = manager.get_server(server_id)

        if not server:
            return jsonify({'error': 'Server not found'}), 404

        queue = get_queue()

        with queue.acquire(RequestPriority.CRITICAL):
            client = ESXiClient(
                host=server['host'],
                user=server['user'],
                password=server['password'],
                port=server['port'],
                verify_ssl=server['verify_ssl']
            )
            client.connect()
            try:
                vm = client.get_vm_by_moid(moid)

                if not vm:
                    return jsonify({'error': f'VM with moid {moid} not found'}), 404

                # Save VM info BEFORE disconnect
                vm_name = vm.name

                ticket = client.acquire_webmks_ticket(vm)

                # Use server['host'] explicitly - ticket.host may be None or hostname
                ticket_data = {
                    'ticket': ticket.ticket,
                    'host': server['host'],  # Use actual server IP/hostname
                    'port': ticket.port or server['port']
                }

                app.logger.info(f"Ticket for server {server_id}: host={server['host']}, port={ticket.port}")
            finally:
                client.disconnect()

        import requests
        proxy_url = f"http://{os.getenv('WS_PROXY_HOST', 'localhost')}:{os.getenv('WS_PROXY_PORT', 8765)}/api/sessions"

        app.logger.info(f"Sending ticket to proxy: {ticket_data}")
        response = requests.post(proxy_url, json=ticket_data, timeout=5)

        if response.status_code != 200:
            return jsonify({'error': 'Failed to create proxy session'}), 500

        session_data = response.json()

        # Use the request host but WebSocket proxy port
        ws_protocol = 'wss' if request.is_secure else 'ws'
        ws_host = request.host.split(':')[0]  # Get hostname without port
        ws_port = os.getenv('WS_PROXY_PORT', 8765)
        ws_url = f"{ws_protocol}://{ws_host}:{ws_port}{session_data['ws_url']}"

        return jsonify({
            'session_id': session_data['session_id'],
            'ws_url': ws_url,
            'vm_name': vm_name,
            'vm_moid': moid,
            'expires_in': 180
        })

    except Exception as e:
        app.logger.error(f"Error creating console session: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/v1/servers/<server_id>/vms/<moid>/thumbnail', methods=['GET'])
def get_server_vm_thumbnail(server_id, moid):
    """
    Get VM thumbnail for specific server.

    CACHE-ONLY endpoint - never hits ESXi directly.
    The background ThumbnailRefreshService continuously updates thumbnails with adaptive rate limiting.
    This endpoint only serves from cache to prevent overwhelming ESXi with direct requests.
    """
    from ..utils.shared_cache import get_thumbnail, get_thumbnail_timestamp, has_thumbnail

    manager = get_server_manager()
    server = manager.get_server(server_id)

    if not server:
        return jsonify({'error': 'Server not found'}), 404

    cache_key = f"thumbnail_{server_id}_{moid}"

    # Check if we have any cached thumbnail (even if stale)
    if has_thumbnail(cache_key):
        thumbnail_data = get_thumbnail(cache_key)
        cache_timestamp = get_thumbnail_timestamp(cache_key)
        age = time.time() - cache_timestamp

        # Serve cached thumbnail regardless of age
        # Background service will keep it fresh
        cache_status = 'HIT' if age < THUMBNAIL_CACHE_TTL else 'STALE'
        app.logger.debug(f"Thumbnail cache {cache_status} for {server_id}/{moid} (age: {age:.1f}s)")

        return Response(
            thumbnail_data,
            mimetype='image/jpeg',
            headers={
                'X-Cache': cache_status,
                'X-Cache-Age': str(int(age))
            }
        )

    # No cache available - background service hasn't populated it yet
    # Return 404 and let background service populate it in next cycle (max 60s wait)
    app.logger.debug(f"Thumbnail not in cache for {server_id}/{moid}, background service will populate")
    return jsonify({
        'error': 'Thumbnail not available yet',
        'message': 'Background service is generating thumbnail, please retry in a few seconds'
    }), 404


@app.route('/api/v1/servers/<server_id>/thumbnails/hashes', methods=['GET'])
def get_thumbnail_hashes(server_id):
    """
    Get MD5 hashes of all cached thumbnails for a server.

    Returns:
        {
            "10": "abc123...",
            "100": "def456...",
            ...
        }
    """
    from ..utils.shared_cache import get_all_thumbnails
    import hashlib

    hashes = {}
    all_thumbnails = get_all_thumbnails()

    # Iterate through all cached thumbnails for this server
    for key, thumbnail_data in all_thumbnails.items():
        if key.startswith(f'thumbnail_{server_id}_'):
            # Extract moid from key: thumbnail_<server_id>_<moid>
            moid = key.split('_', 2)[2]

            # Calculate MD5 hash of thumbnail data
            if thumbnail_data:
                hash_md5 = hashlib.md5(thumbnail_data).hexdigest()
                hashes[moid] = hash_md5

    return jsonify(hashes)


@app.route('/api/v1/vms/<moid>/console', methods=['POST'])
def create_console_session(moid):
    """
    Create WebMKS console session for a VM.

    Args:
        moid: VM managed object ID (e.g., "10")

    Returns:
        {
            "session_id": "uuid",
            "ws_url": "ws://proxy:8765/proxy/uuid",
            "ticket": "...",
            "expires_in": 180
        }
    """
    try:
        queue = get_queue()

        # Console tickets are CRITICAL priority - user is waiting
        with queue.acquire(RequestPriority.CRITICAL):
            with get_connection() as client:
                vm = client.get_vm_by_moid(moid)

                if not vm:
                    return jsonify({'error': f'VM with moid {moid} not found'}), 404

                # Save VM info BEFORE connection closes
                vm_name = vm.name

                # Acquire WebMKS ticket - must be done while connection is active
                ticket = client.acquire_webmks_ticket(vm)

                # Extract ticket info while connection is active
                # Use configured ESXi host - ticket.host may be None or hostname
                ticket_data = {
                    'ticket': ticket.ticket,
                    'host': os.getenv('ESXI_HOST'),
                    'port': ticket.port or int(os.getenv('ESXI_PORT', 443))
                }

        # Create session in proxy (outside ESXi connection context)
        import requests
        proxy_url = f"http://{os.getenv('WS_PROXY_HOST', 'localhost')}:{os.getenv('WS_PROXY_PORT', 8765)}/api/sessions"

        response = requests.post(proxy_url, json=ticket_data, timeout=5)

        if response.status_code != 200:
            return jsonify({'error': 'Failed to create proxy session'}), 500

        session_data = response.json()

        # Use the request host but WebSocket proxy port
        ws_protocol = 'wss' if request.is_secure else 'ws'
        ws_host = request.host.split(':')[0]  # Get hostname without port
        ws_port = os.getenv('WS_PROXY_PORT', 8765)
        ws_url = f"{ws_protocol}://{ws_host}:{ws_port}{session_data['ws_url']}"

        return jsonify({
            'session_id': session_data['session_id'],
            'ws_url': ws_url,
            'vm_name': vm_name,
            'vm_moid': moid,
            'expires_in': 180  # Ticket typically expires in ~3 minutes
        })

    except Exception as e:
        app.logger.error(f"Error creating console session: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/v1/vms/<moid>/thumbnail', methods=['GET'])
def get_vm_thumbnail(moid):
    """
    Get VM console screenshot/thumbnail.

    Returns PNG image with caching (default 10 seconds TTL).
    """
    # Check cache first
    current_time = time.time()
    if moid in THUMBNAIL_CACHE:
        cached = THUMBNAIL_CACHE[moid]
        age = current_time - cached['timestamp']

        if age < THUMBNAIL_CACHE_TTL:
            app.logger.debug(f"Thumbnail cache HIT for VM {moid} (age: {age:.1f}s)")
            return Response(
                cached['data'],
                mimetype='image/png',
                headers={'X-Cache': 'HIT', 'X-Cache-Age': str(int(age))}
            )
        else:
            app.logger.debug(f"Thumbnail cache EXPIRED for VM {moid} (age: {age:.1f}s)")

    # Cache miss or expired - generate new screenshot
    app.logger.debug(f"Thumbnail cache MISS for VM {moid} - generating screenshot")

    # Determine priority: if this is a refresh (had cache), use LOW, otherwise NORMAL
    priority = RequestPriority.LOW if moid in THUMBNAIL_CACHE else RequestPriority.NORMAL

    try:
        queue = get_queue()

        with queue.acquire(priority):
            with get_connection() as client:
                vm = client.get_vm_by_moid(moid)

                if not vm:
                    return jsonify({'error': f'VM with moid {moid} not found'}), 404

                # Check if VM is powered on
                from pyVmomi import vim as pyvim
                if vm.runtime.powerState != pyvim.VirtualMachinePowerState.poweredOn:
                    # Return placeholder for powered-off VMs
                    return jsonify({'error': 'VM is powered off'}), 503

                # Create screenshot
                screenshot_data = client.create_screenshot(vm)

            if screenshot_data:
                # Resize thumbnail for faster loading
                from PIL import Image
                import io

                try:
                    original_size = len(screenshot_data)
                    # Open image from bytes
                    img = Image.open(io.BytesIO(screenshot_data))
                    original_dimensions = img.size

                    # Resize to small thumbnail (max 200x150, maintain aspect ratio)
                    img.thumbnail((200, 150), Image.Resampling.LANCZOS)

                    # Save as JPEG with low quality for smaller size
                    output = io.BytesIO()
                    img.convert('RGB').save(output, format='JPEG', quality=50, optimize=True)
                    resized_data = output.getvalue()
                    resized_size = len(resized_data)

                    reduction_pct = ((original_size - resized_size) / original_size * 100)
                    app.logger.info(f"Thumbnail resized: {original_dimensions} → {img.size}, "
                                   f"{original_size:,} → {resized_size:,} bytes ({reduction_pct:.1f}% reduction)")

                    # Cache resized version
                    THUMBNAIL_CACHE[moid] = {
                        'data': resized_data,
                        'timestamp': current_time
                    }

                    return Response(
                        resized_data,
                        mimetype='image/jpeg',
                        headers={'X-Cache': 'MISS'}
                    )
                except Exception as resize_error:
                    app.logger.warning(f"Failed to resize thumbnail, using original: {resize_error}")
                    # Fallback to original if resize fails
                    THUMBNAIL_CACHE[moid] = {
                        'data': screenshot_data,
                        'timestamp': current_time
                    }

                    return Response(
                        screenshot_data,
                        mimetype='image/png',
                        headers={'X-Cache': 'MISS'}
                    )
            else:
                return jsonify({'error': 'Failed to create screenshot'}), 500

    except Exception as e:
        app.logger.error(f"Error creating thumbnail for VM {moid}: {e}")
        # Return 503 for ESXi overload, 500 for other errors
        if 'Service Unavailable' in str(e) or 'HostConnectFault' in str(e):
            return jsonify({'error': 'ESXi temporarily unavailable'}), 503
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    port = int(os.getenv('FLASK_PORT', 5001))  # Changed default from 5000 to 5001
    debug = os.getenv('FLASK_DEBUG', 'true').lower() == 'true'
    refresh_interval = int(os.getenv('BACKGROUND_REFRESH_INTERVAL', 30))

    print(f"\n{'='*60}")
    print(f"🚀 ESXi WebMKS Console Portal - API Server")
    print(f"{'='*60}")
    print(f"📍 URL: http://localhost:{port}")
    print(f"🔧 Debug mode: {debug}")
    print(f"🖥️  ESXi Host: {os.getenv('ESXI_HOST')}")
    print(f"🔄 Background refresh: {refresh_interval}s interval")
    print(f"{'='*60}\n")

    # Start background refresh service
    refresh_service = get_refresh_service()
    refresh_service.start()
    print(f"✓ Background VM refresh service started\n")

    # Start thumbnail refresh service for all servers
    thumbnail_service = get_thumbnail_service()
    server_manager = get_server_manager()
    servers = server_manager.get_all_servers()

    for server in servers:
        thumbnail_service.start_server_refresh(server['id'], server)
        print(f"✓ Thumbnail refresh started for {server['name']}")

    if servers:
        print()

    try:
        app.run(
            host='0.0.0.0',
            port=port,
            debug=debug,
            threaded=True  # Enable multi-threading for multi-server support
        )
    finally:
        # Stop background service on shutdown
        refresh_service.stop()
        print("\n✓ Background refresh service stopped")
