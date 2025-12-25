# ESXi WebMKS Console Portal

Multi-server ESXi management portal with extreme performance optimizations. Handles 100+ VMs with minimal ESXi load through adaptive rate limiting, intelligent caching, and background processing.

## Features

- 🖥️ **Multi-Server Management** - Manage unlimited ESXi hosts from single interface
- 📸 **Smart Thumbnails** - Real-time VM screenshots with hash-based delta updates
- 🌐 **Browser Console** - One-click noVNC access via WebMKS proxy
- 📊 **Resource Monitoring** - Live CPU/Memory/Disk gauges with 30s cache
- ⚡ **Self-Tuning Performance** - Adaptive rate limiting learns ESXi capacity
- 🛡️ **Production Ready** - Circuit breakers, graceful degradation, error recovery

## Performance

| Metric | Value | Details |
|--------|-------|---------|
| **VM List** | 0.087s | 100+ VMs with background refresh |
| **Thumbnails** | 1.6KB | JPEG compression (200×150, Q50) |
| **Network Traffic** | 400KB/h | 92% reduction vs naive polling |
| **ESXi Load** | Adaptive | Self-tunes from 0.5s to 10s delays |
| **Cache Hit Rate** | >95% | Multi-layer caching strategy |

## Quick Start

```bash
# 1. Copy example configs
cp .env.example .env
cp config/servers.json.example config/servers.json

# 2. Configure ESXi credentials
nano .env
nano config/servers.json

# 3. Deploy
docker-compose up -d

# 4. Access
http://<your-server>:5001
```

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                         Browser                              │
│  ┌────────────────┐  ┌──────────────┐  ┌─────────────────┐ │
│  │ Thumbnail Cache│  │ Hash Checker │  │ noVNC Viewer    │ │
│  │ (instant load) │  │ (delta only) │  │ (WebSocket)     │ │
│  └────────────────┘  └──────────────┘  └─────────────────┘ │
└───────────────┬────────────────┬────────────────┬───────────┘
                │                │                │
         ┌──────▼────────┐  ┌────▼─────┐  ┌──────▼──────┐
         │  Flask API    │  │ REST API │  │  WS Proxy   │
         │  (port 5001)  │  │  Cache   │  │ (port 8765) │
         └───────┬───────┘  └────┬─────┘  └──────┬──────┘
                 │               │                │
         ┌───────▼───────────────▼────────────────▼──────┐
         │         Background Services Layer             │
         │  ┌──────────────┐  ┌────────────────────────┐│
         │  │ VM Refresh   │  │ Thumbnail Refresh      ││
         │  │ (30s cycle)  │  │ (Adaptive rate limit)  ││
         │  └──────────────┘  └────────────────────────┘│
         │  ┌──────────────┐  ┌────────────────────────┐│
         │  │ Request Queue│  │ Circuit Breaker        ││
         │  │ (Priority)   │  │ (Fault tolerance)      ││
         │  └──────────────┘  └────────────────────────┘│
         └──────────────────┬────────────────────────────┘
                            │
                    ┌───────▼────────┐
                    │  ESXi Cluster  │
                    │  (VMware API)  │
                    └────────────────┘
```

## Advanced Optimizations

### 1. Adaptive Rate Limiting with ESXi Feedback

The system learns optimal request rates by analyzing ESXi error responses:

```python
# When ESXi returns 503 or timeout:
- Track: requests_sent, time_window, error_message
- Parse: timeout value from error (if available)
- Calculate: optimal_delay = timeout / (requests * 0.8)
- Apply: exponential backoff (×2 on error, ×0.9 on success)
- Range: 0.5s (fast) → 10s (throttled)
```

**Result:** System automatically finds the sweet spot for each ESXi host.

### 2. Multi-Layer Caching Strategy

| Layer | TTL | Purpose | Fallback |
|-------|-----|---------|----------|
| Browser | ∞ | Instant tab switching | - |
| Hash Map | 30s | Delta detection | Full refresh |
| Thumbnail | 120s | Reduce ESXi calls | Stale data |
| VM List | 60s | Background refresh | Circuit breaker |
| Stats | 30s | Gauge updates | Last known value |

**Graceful Degradation:** On ESXi overload, serves stale cache with warning indicator.

### 3. Hash-Based Delta Updates

```javascript
// Frontend checks hashes before downloading
const serverHashes = await fetch('/thumbnails/hashes');
for (const [moid, newHash] of Object.entries(serverHashes)) {
    if (thumbnailHashes[moid] !== newHash) {
        // Only download changed thumbnails
        await refreshThumbnail(serverId, moid);
    }
}
```

**Bandwidth Savings:** 92% reduction (400KB vs 4.8MB per hour for 100 VMs)

### 4. Background Refresh Services

**VM List Refresh (30s cycle):**
- Fetches all VM data in background
- Updates cache atomically
- Frontend always reads from cache (no wait)

**Thumbnail Refresh (adaptive):**
- Processes VMs in batches of 2
- Tracks request timestamps
- Adjusts delay based on 503 errors
- Validates images before cache update
- Preserves old thumbnails on errors

### 5. Request Queue with Priorities

```python
class RequestPriority(Enum):
    CRITICAL = 1   # Console tickets (user waiting)
    HIGH = 2       # First-time cache miss
    NORMAL = 3     # Background refresh
    LOW = 4        # Stats polling
```

Ensures interactive requests never wait for background tasks.

### 6. Circuit Breaker Pattern

```python
# Protects against cascading failures
- Failure threshold: 5 consecutive errors
- Recovery timeout: 30 seconds
- Success threshold: 3 requests to close
```

### 7. Connection Pooling

- Reuses ESXi API connections for 5 minutes
- Pool size: 5 connections per server
- Reduces SSL handshake overhead

### 8. Smart Image Validation

```python
# Prevents black squares on errors
if screenshot_data and len(screenshot_data) > 0:
    img = Image.open(BytesIO(screenshot_data))
    if img.size[0] > 0 and img.size[1] > 0:
        resized = compress_jpeg(img)
        if len(resized) > 100:  # Valid JPEG
            update_cache(resized)  # Only then update
        else:
            keep_old_thumbnail()   # Preserve previous
```

## Configuration

### Environment Variables (.env)

```bash
# API Ports
FLASK_PORT=5001
WS_PROXY_PORT=8765

# Cache TTLs
THUMBNAIL_CACHE_TTL=120
VM_LIST_CACHE_TTL=60

# Background Services
BACKGROUND_REFRESH_INTERVAL=30

# Rate Limiting (auto-adjusted)
ESXI_MAX_CONCURRENT=12
ESXI_MIN_INTERVAL=0.15

# Circuit Breaker
CB_FAILURE_THRESHOLD=5
CB_RECOVERY_TIMEOUT=30
```

### Multi-Server Config (config/servers.json)

```json
{
  "servers": {
    "uuid-1": {
      "name": "Production ESXi",
      "host": "192.168.1.10",
      "port": 443,
      "user": "root",
      "password": "secret",
      "verify_ssl": false,
      "enabled": true
    }
  }
}
```

## Deployment

### Docker (Recommended)

```bash
# Build and start
docker-compose up -d

# View logs with adaptive rate limiting info
docker-compose logs -f | grep -E "delay|rate limit|cycle"

# Monitor performance
docker-compose exec esxi-console-portal curl http://localhost:5001/api/v1/queue/stats
```

### Manual (Development)

```bash
# Install dependencies
pip install -r requirements.txt

# Start services
python -m src.api.app &           # Flask API
python -m src.ws_proxy.webmks_proxy &  # WebSocket Proxy
```

## Monitoring

### Log Patterns

**Adaptive Rate Limiting:**
```
ESXi rate limit for server-123: 5 requests in 2.3s, increasing delay to 1.00s
Thumbnail refresh cycle: 45/69 VMs in 98.5s, delay: 1.00s, errors: 2
```

**Cache Performance:**
```
Returning stale stats cache for server-123 due to error
Batch for server-123: 65 succeeded, 4 failed, delay: 0.75s, 3 measurements
```

**Circuit Breaker:**
```
Circuit breaker OPEN for server-123 after 5 failures
Circuit breaker CLOSED for server-123 after 3 successes
```

### API Endpoints

```bash
# Queue statistics
GET /api/v1/queue/stats

# Thumbnail hashes (for delta detection)
GET /api/v1/servers/{id}/thumbnails/hashes

# Health check
GET /api/v1/health
```

## Troubleshooting

### High ESXi Load

**Symptoms:** Lots of 503 errors, slow responses

**Solution:** Adaptive rate limiting will auto-adjust, but you can also:
```bash
# Increase minimum delay in .env
ESXI_MIN_INTERVAL=0.5

# Reduce concurrent requests
ESXI_MAX_CONCURRENT=8
```

### Thumbnails Not Updating

**Check logs:**
```bash
docker-compose logs -f | grep thumbnail
```

**Common causes:**
- VMs powered off (normal, no thumbnail generated)
- ESXi screenshot API disabled
- Rate limiting active (check "delay: X.XXs" in logs)

### WebSocket Connection Failed

**Check ports:**
```bash
# Both ports must be accessible
curl http://your-server:5001/api/v1/health  # Flask API
curl http://your-server:8765/                # WebSocket Proxy
```

**If using reverse proxy:** Configure WebSocket upgrade headers:
```nginx
location /proxy/ {
    proxy_pass http://localhost:8765;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
}
```

## Performance Tuning

### For Large Deployments (200+ VMs)

```bash
# .env adjustments
VM_LIST_CACHE_TTL=90              # Refresh less often
THUMBNAIL_CACHE_TTL=180           # Longer cache
BACKGROUND_REFRESH_INTERVAL=45   # Slower background refresh
ESXI_MAX_CONCURRENT=6             # More conservative
```

### For Multiple ESXi Hosts

Each server gets independent:
- Background refresh thread
- Thumbnail refresh thread with own adaptive delays
- Circuit breaker instance
- Request queue

**Scales linearly** with number of servers.

## Security

- SSL warnings disabled for self-signed ESXi certs
- Credentials stored in gitignored files
- No credential logging
- Session timeout: 180s
- WebMKS ticket timeout: 60s

## License

MIT

---

**Built with:** Flask, noVNC, pyVmomi, aiohttp, Pillow
**Tested with:** ESXi 6.7, 7.0, 8.0
**Max tested:** 101 VMs on single ESXi host
