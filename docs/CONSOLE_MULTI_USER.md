# Multi-User Console Support

## Обзор

Система поддерживает **одновременное открытие консолей несколькими пользователями** благодаря:

1. **ESXi Connection Pool** - пул постоянных подключений к ESXi
2. **Priority Queue** - CRITICAL приоритет для консолей
3. **WebSocket Proxy** - поддержка множественных WebMKS сессий

## Архитектура

```
┌────────────────────────────────────────────────────┐
│           Multiple Users (1..N)                    │
└────────────────────────────────────────────────────┘
                    │
     ┌──────────────┼──────────────┐
     │              │              │
   User 1        User 2        User 3
     │              │              │
     └──────────────┴──────────────┘
                    │
            [Click "Open Console"]
                    │
                    ↓
┌────────────────────────────────────────────────────┐
│  POST /api/v1/vms/{moid}/console (CRITICAL)       │
└────────────────────────────────────────────────────┘
                    │
                    ↓
┌────────────────────────────────────────────────────┐
│         Priority Queue (CRITICAL = 0)              │
│  • Консоли обрабатываются первыми                 │
│  • avg_wait_time ~0s                               │
└────────────────────────────────────────────────────┘
                    │
                    ↓
┌────────────────────────────────────────────────────┐
│          ESXi Connection Pool                      │
│  • Pool size: 5 connections                        │
│  • TTL: 300s (5 minutes)                           │
│  • Lazy initialization (on-demand)                 │
│  • Auto-reconnect on failure                       │
└────────────────────────────────────────────────────┘
                    │
                    ↓
┌────────────────────────────────────────────────────┐
│          ESXi API                                   │
│  • get_vm_by_moid()                                │
│  • acquire_webmks_ticket()                         │
│  • Returns: {ticket, host, port}                   │
└────────────────────────────────────────────────────┘
                    │
                    ↓
┌────────────────────────────────────────────────────┐
│      WebSocket Proxy (Multi-session)               │
│  • POST /api/sessions → Create session             │
│  • GET /proxy/{session_id} → WebSocket             │
│  • Each session = isolated WebMKS connection       │
└────────────────────────────────────────────────────┘
                    │
     ┌──────────────┼──────────────┐
     │              │              │
Session 1       Session 2      Session 3
  (VM 10)        (VM 11)        (VM 12)
     │              │              │
     └──────────────┴──────────────┘
                    │
                    ↓
         noVNC Client (Browser)
```

## Решенные проблемы

### Проблема 1: NotAuthenticated при создании консоли

**Симптомы:**
```
Error: vim.fault.NotAuthenticated
msg: 'The session is not authenticated.'
HTTP 500
```

**Причина:**
- ESXiClient использовал context manager (`with`)
- Connection закрывался (`disconnect()`) до использования ticket
- VM object становился недействительным

**Решение:**
```python
# ❌ НЕПРАВИЛЬНО
with ESXiClient() as client:
    vm = client.get_vm_by_moid(moid)
    ticket = client.acquire_webmks_ticket(vm)
# client.disconnect() вызывается здесь - ticket недействителен!

# ✅ ПРАВИЛЬНО
with get_connection() as client:  # Из connection pool
    vm = client.get_vm_by_moid(moid)
    ticket = client.acquire_webmks_ticket(vm)
    # Извлекаем данные пока connection активен
    ticket_data = {
        'ticket': ticket.ticket,
        'host': ticket.host,
        'port': ticket.port
    }
# Connection возвращается в pool (не закрывается!)
# ticket_data валиден
```

### Проблема 2: Перегрузка ESXi при multi-user

**Симптомы:**
- Каждый пользователь создает новое ESXi подключение
- 10 пользователей = 10 × (1 VM list + 70 thumbnails) = 710 запросов
- ESXi возвращает 503 Service Unavailable

**Решение:**
1. **Connection Pool** - переиспользование подключений
2. **VM List Cache** (TTL=30s) - один запрос вместо N
3. **Thumbnail Cache** (TTL=120s) - shared между пользователями
4. **Priority Queue** - CRITICAL для консолей, LOW для background

## ESXi Connection Pool

### Конфигурация

```bash
# .env
ESXI_POOL_SIZE=5              # Максимум 5 одновременных подключений
ESXI_CONNECTION_TTL=300       # Подключение живет 5 минут
```

### Как работает

1. **Lazy Initialization**
   - Pool создается пустым
   - Connections создаются on-demand при первом запросе

2. **Connection Lifecycle**
   ```
   Request → Pool.get(timeout=0.1)
             ↓
      Empty? → Create new connection
             ↓
   Connection → Validate (check TTL, test CurrentTime())
             ↓
      Valid? → Use it
      Invalid? → Reconnect
             ↓
   After use → Pool.put() (return to pool)
   ```

3. **Auto-Reconnect**
   - If connection fails validation → auto-reconnect
   - If reconnect fails → error (counted in stats)

### Статистика Pool

```json
{
  "pool": {
    "pool_size": 5,
    "total_connections": 3,      // Созданы on-demand
    "available_connections": 2,   // Свободны сейчас
    "active_connections": 1,      // Используются сейчас
    "reconnects": 0,              // Автоматических переподключений
    "errors": 0                   // Ошибок
  }
}
```

### Capacity Planning

**Максимальное количество пользователей:**

```
Допустим:
- ESXI_POOL_SIZE = 5
- Каждая консоль = 1 connection

Максимум одновременных консолей = 5

Но благодаря быстрому возврату в pool:
- Console ticket request: ~0.5s
- Connection освобождается сразу
- Следующий пользователь берет тот же connection

Фактически: 10-20 пользователей комфортно
```

## WebSocket Proxy (Multi-session)

WebSocket proxy уже поддерживает множественные сессии:

```python
# src/ws_proxy/webmks_proxy.py

# Session storage
sessions = {}  # {session_id: {ticket, host, port}}

# Each WebSocket connection = separate session
async def proxy_handler(websocket, session_id):
    session = sessions[session_id]
    # Isolated ESXi connection for this session
    async with websockets.connect(
        f"wss://{session['host']}:{session['port']}/ticket/{session['ticket']}"
    ) as esxi_ws:
        # Bidirectional proxy
        await asyncio.gather(
            forward_to_esxi(websocket, esxi_ws),
            forward_to_client(esxi_ws, websocket)
        )
```

### Мониторинг сессий

WebSocket proxy логирует каждую созданную сессию:

```
2025-12-24 22:57:41 - INFO - Created session 2e757a40-... (User 1)
2025-12-24 22:57:41 - INFO - Created session 5f7eb81a-... (User 2)
2025-12-24 22:57:41 - INFO - Created session 6bef73a9-... (User 3)
```

## Тестирование Multi-User

### Тест 1: 3 пользователя открывают консоли одновременно

```bash
# Simulate 3 users
curl -X POST http://localhost:5001/api/v1/vms/10/console &
curl -X POST http://localhost:5001/api/v1/vms/11/console &
curl -X POST http://localhost:5001/api/v1/vms/12/console &
wait
```

**Результат:**
```json
User 1: {"session_id": "6bef73a9-...", "ws_url": "ws://localhost:8765/proxy/6bef73a9-..."}
User 2: {"session_id": "2e757a40-...", "ws_url": "ws://localhost:8765/proxy/2e757a40-..."}
User 3: {"session_id": "5f7eb81a-...", "ws_url": "ws://localhost:8765/proxy/5f7eb81a-..."}
```

✅ Все 3 консоли созданы успешно

### Тест 2: Проверка приоритета

```bash
# Start loading 70 thumbnails (NORMAL/LOW priority)
for moid in {1..70}; do
    curl http://localhost:5001/api/v1/vms/${moid}/thumbnail &
done

# User opens console (CRITICAL priority) - should be fast
time curl -X POST http://localhost:5001/api/v1/vms/10/console
```

**Результат:**
```
real    0m0.452s    # Console opened in < 0.5s
                    # Even while 70 thumbnails loading!
```

✅ CRITICAL priority работает - консоли не блокируются

### Тест 3: Connection Pool под нагрузкой

```bash
# Stats before
curl http://localhost:5001/api/v1/queue/stats

# Create 10 consoles rapidly
for i in {10..19}; do
    curl -X POST http://localhost:5001/api/v1/vms/${i}/console &
done
wait

# Stats after
curl http://localhost:5001/api/v1/queue/stats
```

**Результат:**
```json
{
  "pool": {
    "total_connections": 5,       // Pool size limit reached
    "available_connections": 4,   // Reused connections
    "reconnects": 0,              // No failures
    "errors": 0
  },
  "queue": {
    "by_priority": {
      "CRITICAL": {
        "total_requests": 10,     // All console requests
        "avg_wait_time": 0.03     // ~30ms average
      }
    }
  }
}
```

✅ Pool переиспользует connections эффективно

## Проблемы и решения

### Проблема: "No ESXi connections available in pool"

**Причина:**
- Pool не смог создать initial connections
- Все connections в use, timeout

**Решение:**
1. Проверить credentials в `.env`
2. Увеличить `ESXI_POOL_SIZE`
3. Уменьшить concurrent load

### Проблема: WebSocket disconnects после 20 секунд

**Причина:**
- `ping_interval=20` в proxy

**Решение:**
```python
# src/ws_proxy/webmks_proxy.py
async with websockets.connect(
    webmks_url,
    ping_interval=None,    # Disable auto-ping
    ping_timeout=None
) as esxi_ws:
```

### Проблема: Консоли медленно открываются

**Диагностика:**
```bash
curl http://localhost:5001/api/v1/queue/stats | grep CRITICAL
```

**Должно быть:**
- `avg_wait_time`: < 0.1s

**Если больше:**
1. Увеличить `ESXI_MAX_CONCURRENT`
2. Увеличить `ESXI_POOL_SIZE`
3. Проверить network latency к ESXi

## Best Practices

1. **Connection Pool Size**
   ```bash
   # Для 5-10 пользователей
   ESXI_POOL_SIZE=5

   # Для 10-20 пользователей
   ESXI_POOL_SIZE=10
   ```

2. **Мониторинг**
   - Отслеживать `pool.errors` и `pool.reconnects`
   - Alerting если `pool.available_connections = 0` постоянно

3. **Graceful Degradation**
   - Если pool exhausted → показать пользователю "Все консоли заняты"
   - Retry через несколько секунд

4. **Session Management**
   - WebSocket proxy должен cleanup старые сессии
   - Timeout неактивных консолей

## Performance Metrics

Реальные метрики (70 VMs, 3 concurrent users):

| Метрика | Значение | Описание |
|---------|----------|----------|
| **Console Open Time** | 0.45s | Время открытия консоли |
| **CRITICAL avg_wait** | 0.04s | Среднее ожидание в очереди |
| **Pool connections** | 3/5 | Используется 3 из 5 |
| **Pool errors** | 0 | Нет ошибок подключения |
| **Concurrent consoles** | 3 | Одновременно открыто |

## Заключение

Система поддерживает **5-10 одновременных пользователей** с открытыми консолями благодаря:

✅ **ESXi Connection Pool** - переиспользование подключений
✅ **Priority Queue** - CRITICAL приоритет для консолей
✅ **WebSocket Proxy** - изолированные сессии
✅ **Smart Caching** - снижение нагрузки на ESXi

Для большего количества пользователей - увеличьте `ESXI_POOL_SIZE` и `ESXI_MAX_CONCURRENT`.
