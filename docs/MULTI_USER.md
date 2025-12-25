# Multi-User Support

## Обзор

Система поддерживает одновременную работу **нескольких пользователей** без перегрузки ESXi сервера благодаря:

1. **Приоритетной очереди запросов**
2. **Умному кэшированию** (thumbnails + VM list)
3. **Оптимизированным лимитам** для concurrent requests

## Архитектура для Multi-User

### Система приоритетов

```
CRITICAL (0) - Открытие консоли, критические операции
    ↓ Обрабатываются первыми, практически без ожидания
HIGH (1)     - Загрузка списка VM
    ↓ Высокий приоритет, кэшируется на 30 секунд
NORMAL (2)   - Первая загрузка thumbnails
    ↓ Нормальный приоритет, кэшируется на 120 секунд
LOW (3)      - Обновление thumbnails
    ↓ Низкий приоритет, выполняется когда есть свободные слоты
```

### Двухуровневое кэширование

**1. VM List Cache (глобальный)**
- TTL: 30 секунд
- Все пользователи получают один и тот же список
- Reduces ESXi load: вместо N запросов (N пользователей) → 1 запрос / 30 сек

**2. Thumbnail Cache (глобальный)**
- TTL: 120 секунд
- Shared между всеми пользователями
- Первый пользователь загружает → остальные получают из кэша

## Конфигурация

### Оптимальные настройки для 3-5 пользователей:

```bash
# .env

# Queue settings
ESXI_MAX_CONCURRENT=8      # 8 одновременных запросов к ESXi
ESXI_MIN_INTERVAL=0.05     # 50ms минимум между запросами

# VM list cache (shared)
VM_LIST_CACHE_TTL=30       # 30 секунд кэш списка VM

# Thumbnail cache (shared)
THUMBNAIL_CACHE_TTL=120    # 2 минуты кэш thumbnails
```

### Для 5-10 пользователей:

```bash
ESXI_MAX_CONCURRENT=12
ESXI_MIN_INTERVAL=0.05
VM_LIST_CACHE_TTL=45
THUMBNAIL_CACHE_TTL=180
```

### Для 10+ пользователей:

```bash
ESXI_MAX_CONCURRENT=16
ESXI_MIN_INTERVAL=0.05
VM_LIST_CACHE_TTL=60
THUMBNAIL_CACHE_TTL=300
```

## Сценарии использования

### Сценарий 1: 3 пользователя открывают страницу одновременно

```
User 1 → GET /api/v1/vms
         ├─ Cache MISS → ESXi query (HIGH priority)
         ├─ Cache VM list (30s TTL)
         └─ Returns 70 VMs

User 2 → GET /api/v1/vms (0.5s later)
         ├─ Cache HIT → Return from cache
         └─ No ESXi query!

User 3 → GET /api/v1/vms (1s later)
         ├─ Cache HIT → Return from cache
         └─ No ESXi query!
```

**Результат**: Вместо 3 запросов к ESXi → 1 запрос

### Сценарий 2: 3 пользователя запрашивают thumbnails

70 VM × 3 пользователя = 210 потенциальных запросов

```
User 1 loads page at T+0s:
  - Requests 70 thumbnails
  - Queue processes 8 concurrent
  - Rest waits in queue (NORMAL priority)
  - Thumbnails cached (120s TTL)

User 2 loads page at T+10s:
  - Requests 70 thumbnails
  - Cache HIT for all 70 → No ESXi queries!
  - Instant load

User 3 loads page at T+20s:
  - Requests 70 thumbnails
  - Cache HIT for all 70 → No ESXi queries!
  - Instant load
```

**Результат**: Вместо 210 запросов → 70 запросов (только User 1)

### Сценарий 3: Пользователь открывает консоль во время загрузки thumbnails

```
Background: 70 thumbnails loading (NORMAL/LOW priority)
  ├─ 8 active requests
  └─ 62 waiting in queue

User clicks "Open Console":
  ├─ POST /api/v1/vms/10/console (CRITICAL priority)
  ├─ Jumps to front of queue!
  ├─ Processed immediately
  └─ Console opens in < 0.5s

Background thumbnails continue after console
```

**Результат**: CRITICAL запросы не ждут, даже при загрузке

## Статистика производительности

### Реальные метрики (из тестов):

```json
{
  "queue": {
    "max_concurrent": 8,
    "min_interval": 0.05,
    "total_requests": 295,
    "avg_wait_time": 0.009,
    "by_priority": {
      "CRITICAL": {
        "total_requests": 2,
        "avg_wait_time": 0.0
      },
      "HIGH": {
        "total_requests": 16,
        "avg_wait_time": 0.01
      },
      "NORMAL": {
        "total_requests": 73,
        "avg_wait_time": 0.018
      },
      "LOW": {
        "total_requests": 205,
        "avg_wait_time": 0.006
      }
    }
  },
  "cache": {
    "thumbnail_count": 69,
    "thumbnail_ttl": 120
  }
}
```

### Анализ:

- **CRITICAL**: 0.0s ожидание - мгновенно
- **HIGH**: 0.01s ожидание - очень быстро
- **NORMAL**: 0.018s ожидание - быстро
- **LOW**: 0.006s ожидание - выполняется в простое

## Мониторинг

### Endpoint для статистики:

```bash
curl http://localhost:5001/api/v1/queue/stats
```

### Ключевые метрики для мониторинга:

1. **active_requests** - текущая нагрузка
   - Норма: 0-8 (при max_concurrent=8)
   - Тревога: постоянно = max_concurrent

2. **waiting_requests** - размер очереди
   - Норма: 0-20
   - Тревога: > 50

3. **avg_wait_time** - среднее время ожидания
   - Норма: < 0.1s
   - Тревога: > 1s

4. **by_priority.CRITICAL.avg_wait_time**
   - Должен быть: ~ 0s
   - Критично если: > 0.5s

## Capacity Planning

### Расчет максимального числа пользователей:

```
Пусть:
- N = количество пользователей
- V = количество VM (70)
- C = max_concurrent (8)
- I = min_interval (0.05s)
- T_cache = VM_LIST_CACHE_TTL (30s)
- T_thumb = THUMBNAIL_CACHE_TTL (120s)

Worst case (все пользователи одновременно):
1. VM list: 1 запрос (благодаря кэшу)
2. Thumbnails: V запросов (первый пользователь)
               0 запросов (остальные из кэша)

Time to load = V / C * I = 70 / 8 * 0.05 = 0.44s

Максимальное кол-во пользователей в течение T_thumb:
N_max = без ограничений (кэш shared)

Практически: 10-20 пользователей комфортно
```

### Признаки необходимости масштабирования:

1. `avg_wait_time > 1s` постоянно
2. `waiting_requests > 100` пиково
3. `CRITICAL.avg_wait_time > 0.5s`
4. Жалобы пользователей на медленную загрузку

### Варианты масштабирования:

**Вертикальное (увеличить лимиты):**
```bash
ESXI_MAX_CONCURRENT=16      # было 8
VM_LIST_CACHE_TTL=60        # было 30
THUMBNAIL_CACHE_TTL=300     # было 120
```

**Оптимизация кэширования:**
```bash
# Увеличить TTL для статичных данных
VM_LIST_CACHE_TTL=120       # VMs редко меняются
THUMBNAIL_CACHE_TTL=600     # 10 минут для thumbnails
```

**Redis для shared cache (будущее):**
- Shared cache между несколькими Flask инстансами
- Позволяет horizontal scaling

## Troubleshooting

### Проблема: Медленная загрузка для второго пользователя

**Диагностика:**
```bash
# Проверить cache hit rate
curl http://localhost:5001/api/v1/vms | grep cached
# Должно быть: "cached": true
```

**Решение:** Увеличить TTL кэшей

### Проблема: Консоль открывается медленно

**Диагностика:**
```bash
# Проверить CRITICAL priority
curl http://localhost:5001/api/v1/queue/stats | grep -A2 CRITICAL
# avg_wait_time должен быть ~0
```

**Решение:** Убедиться, что `ESXI_MAX_CONCURRENT` достаточно велик (минимум 4-8)

### Проблема: ESXi все еще перегружен

**Диагностика:**
```bash
# Проверить total_requests
curl http://localhost:5001/api/v1/queue/stats | grep total_requests
```

**Решение:**
1. Увеличить все TTL кэшей
2. Уменьшить `ESXI_MAX_CONCURRENT`
3. Увеличить `ESXI_MIN_INTERVAL`

## Best Practices

1. **Кэширование прежде всего**
   - Длинные TTL для редко меняющихся данных
   - Shared кэш между пользователями

2. **Приоритизация правильная**
   - CRITICAL только для user-facing операций
   - LOW для background обновлений

3. **Мониторинг постоянно**
   - Отслеживать queue stats
   - Alerting на высокие wait times

4. **Graceful degradation**
   - Если ESXi перегружен → старые thumbnails из кэша
   - Если VM list недоступен → показать cached версию

5. **Frontend оптимизация**
   - Staggered loading (400ms между VM)
   - Lazy loading thumbnails (только видимые)
   - Debounce search queries

## Примеры использования API

### Python client для multi-user тестирования:

```python
import requests
import concurrent.futures
import time

API_BASE = "http://localhost:5001/api/v1"

def simulate_user(user_id):
    """Simulate one user loading the page"""
    print(f"User {user_id}: Loading VM list...")
    start = time.time()

    # Load VM list
    resp = requests.get(f"{API_BASE}/vms")
    data = resp.json()
    cached = data.get('cached', False)

    print(f"User {user_id}: Got {len(data['vms'])} VMs "
          f"(cached={cached}) in {time.time()-start:.2f}s")

    # Load thumbnails for first 10 VMs
    for vm in data['vms'][:10]:
        requests.get(f"{API_BASE}/vms/{vm['moid']}/thumbnail")

    print(f"User {user_id}: Loaded 10 thumbnails in {time.time()-start:.2f}s")
    return time.time() - start

# Simulate 5 users simultaneously
with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
    futures = [executor.submit(simulate_user, i) for i in range(1, 6)]
    times = [f.result() for f in futures]

print(f"Average load time: {sum(times)/len(times):.2f}s")
print(f"Max load time: {max(times):.2f}s")
print(f"Min load time: {min(times):.2f}s")
```

## Заключение

Система способна обслуживать **3-10 пользователей одновременно** без перегрузки ESXi благодаря:

✅ Приоритетной очереди (CRITICAL запросы обрабатываются мгновенно)
✅ Shared кэшированию (users 2-N получают данные из кэша)
✅ Умным лимитам (8 concurrent, 0.05s interval)
✅ Graceful degradation (старые данные из кэша при перегрузке)

Для большего количества пользователей - увеличьте лимиты и TTL кэшей.
