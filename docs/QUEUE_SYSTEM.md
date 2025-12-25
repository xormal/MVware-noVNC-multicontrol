# ESXi Request Queue System

## Описание

Система очередей для управления запросами к ESXi API с целью предотвращения перегрузки сервера.

## Возможности

- **Контроль параллельности**: Ограничение количества одновременных запросов к ESXi
- **Rate Limiting**: Минимальный интервал между запросами
- **Автоматическая очередь**: Запросы автоматически ставятся в очередь при достижении лимита
- **Статистика**: Мониторинг активных/ожидающих запросов и времени ожидания
- **Thread-safe**: Безопасна для использования в многопоточной среде

## Конфигурация

Параметры в `.env`:

```bash
# Максимальное количество одновременных запросов к ESXi
ESXI_MAX_CONCURRENT=3

# Минимальный интервал между запросами (секунды)
ESXI_MIN_INTERVAL=0.1
```

### Рекомендуемые значения

- **Для ESXi с большой нагрузкой** (70+ VM):
  - `ESXI_MAX_CONCURRENT=3`
  - `ESXI_MIN_INTERVAL=0.1`

- **Для ESXi с низкой нагрузкой** (< 20 VM):
  - `ESXI_MAX_CONCURRENT=5`
  - `ESXI_MIN_INTERVAL=0.05`

- **Для высокопроизводительных серверов**:
  - `ESXI_MAX_CONCURRENT=10`
  - `ESXI_MIN_INTERVAL=0.05`

## Использование

### В Flask API (автоматически)

Все endpoints автоматически используют очередь:

- `GET /api/v1/vms` - Список VM
- `GET /api/v1/vms/<moid>/thumbnail` - Thumbnails
- `POST /api/v1/vms/<moid>/console` - Создание консоли

### Мониторинг очереди

```bash
curl http://localhost:5001/api/v1/queue/stats
```

**Ответ:**
```json
{
  "queue": {
    "max_concurrent": 3,
    "min_interval": 0.1,
    "active_requests": 2,
    "waiting_requests": 1,
    "total_requests": 145,
    "avg_wait_time": 2.34
  },
  "cache": {
    "thumbnail_count": 45,
    "thumbnail_ttl": 120
  }
}
```

### Программное использование

```python
from src.utils.request_queue import get_queue

queue = get_queue()

# Вариант 1: Context manager
with queue.acquire():
    result = esxi_client.get_vms()

# Вариант 2: Execute wrapper
result = queue.execute(esxi_client.get_vms)

# Статистика
stats = queue.get_stats()
print(f"Active: {stats['active_requests']}")
print(f"Waiting: {stats['waiting_requests']}")
```

## Метрики

- **max_concurrent**: Максимальное количество одновременных запросов (из конфигурации)
- **min_interval**: Минимальный интервал между запросами в секундах (из конфигурации)
- **active_requests**: Количество текущих активных запросов к ESXi
- **waiting_requests**: Количество запросов, ожидающих в очереди
- **total_requests**: Общее количество обработанных запросов с момента запуска
- **avg_wait_time**: Среднее время ожидания в очереди (секунды)

## Как это работает

1. **Запрос поступает** → Проверяется доступность слота в семафоре
2. **Слот занят** → Запрос ждет в очереди (`waiting_requests++`)
3. **Слот освободился** → Запрос становится активным (`active_requests++`)
4. **Rate limiting** → Ожидание `min_interval` секунд с момента последнего запроса
5. **Выполнение** → Запрос к ESXi API
6. **Завершение** → Освобождение слота (`active_requests--`)

## Примеры работы

### Сценарий 1: Загрузка страницы с 70 VM

```
Frontend запрашивает 70 thumbnails одновременно
  ↓
Только 3 запроса выполняются параллельно (ESXI_MAX_CONCURRENT=3)
  ↓
67 запросов ждут в очереди (waiting_requests=67)
  ↓
По мере завершения, новые запросы берутся из очереди
  ↓
Минимум 0.1 секунды между каждым запросом (ESXI_MIN_INTERVAL=0.1)
```

**Результат**: ESXi получает максимум 3 одновременных запроса вместо 70, предотвращая ошибки 503.

### Сценарий 2: Периодическое обновление thumbnails

```
70 powered-on VMs обновляют thumbnails каждые 120 секунд
  ↓
Staggered loading: 400ms между VM (frontend)
  ↓
Запросы растянуты на ~30 секунд
  ↓
Очередь обрабатывает по 3 запроса одновременно
  ↓
Плавная нагрузка на ESXi без пиков
```

## Troubleshooting

### Слишком долгое ожидание (avg_wait_time > 10 секунд)

**Решение**: Увеличьте `ESXI_MAX_CONCURRENT`:
```bash
ESXI_MAX_CONCURRENT=5
```

### ESXi все еще перегружен (503 ошибки)

**Решение**: Уменьшите параллельность и увеличьте интервал:
```bash
ESXI_MAX_CONCURRENT=2
ESXI_MIN_INTERVAL=0.2
```

### Thumbnails обновляются слишком медленно

**Решение**: Увеличьте `THUMBNAIL_CACHE_TTL`, чтобы уменьшить частоту запросов:
```bash
THUMBNAIL_CACHE_TTL=180  # 3 минуты
```

## Интеграция с кэшированием

Система очередей работает совместно с кэшированием thumbnails:

1. **Cache HIT** → Очередь не используется (thumbnail из кэша)
2. **Cache MISS** → Запрос через очередь к ESXi API
3. **Cache TTL** → После истечения TTL, новый запрос через очередь

Оптимальное соотношение:
- `THUMBNAIL_CACHE_TTL=120` (2 минуты кэш)
- `THUMBNAIL_REFRESH_INTERVAL=120` (2 минуты между обновлениями)
- `ESXI_MAX_CONCURRENT=3` (максимум 3 одновременных запроса)

## Архитектура

```
┌─────────────┐
│   Browser   │
└──────┬──────┘
       │ (70 thumbnail requests)
       ↓
┌─────────────────────────────────┐
│      Flask API Endpoints        │
│  /api/v1/vms/<moid>/thumbnail  │
└──────┬──────────────────────────┘
       │
       ↓
┌─────────────────────────────────┐
│    Thumbnail Cache (TTL=120s)   │
│      Cache HIT? → Return        │
│      Cache MISS? ↓              │
└──────┬──────────────────────────┘
       │
       ↓
┌─────────────────────────────────┐
│    ESXi Request Queue           │
│  • Semaphore (max_concurrent=3) │
│  • Rate Limiter (min_interval)  │
│  • Wait in queue if full        │
└──────┬──────────────────────────┘
       │ (max 3 concurrent)
       ↓
┌─────────────────────────────────┐
│         ESXi API                │
│  • CreateScreenshot_Task()      │
│  • Download screenshot image    │
└─────────────────────────────────┘
```

## Логирование

Очередь автоматически отслеживает метрики. Для детального логирования:

```python
import logging

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger('request_queue')

# Метрики будут в stats endpoint
stats = queue.get_stats()
logger.info(f"Queue stats: {stats}")
```

## Best Practices

1. **Мониторинг**: Регулярно проверяйте `/api/v1/queue/stats`
2. **Настройка под нагрузку**: Адаптируйте `ESXI_MAX_CONCURRENT` под ваш ESXi сервер
3. **Кэширование**: Используйте достаточный `THUMBNAIL_CACHE_TTL` для снижения запросов
4. **Staggered loading**: Frontend должен растягивать запросы во времени
5. **Production mode**: Всегда используйте `FLASK_DEBUG=false` в продакшене
