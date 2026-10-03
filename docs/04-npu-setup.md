# Настройка NPU RK3588

## Что уже работает

- Драйвер NPU **встроен в ядро** (`CONFIG_ROCKCHIP_RKNPU=y`), версия **0.9.2**
- NPU виден как DRM render-node: `/dev/dri/renderD129`
- `librknnrt.so` версии **1.5.2** — совместима с драйвером 0.9.2

Проверка:
```bash
zcat /proc/config.gz | grep -i rknpu
# CONFIG_ROCKCHIP_RKNPU=y
# CONFIG_ROCKCHIP_RKNPU_DRM_GEM=y

sudo cat /sys/kernel/debug/rknpu/version
# RKNPU driver: v0.9.2

strings /usr/lib/librknnrt.so | grep 'librknnrt version:'
# librknnrt version: 1.5.2 (c6b7b351a@2023-08-23T15:28:22)

ls -la /dev/dri/
# card0  card1  renderD128  renderD129
```

## Установка librknnrt.so

Скачивается из тега `v1.5.2` репозитория rockchip-linux/rknpu2:

```bash
cd ~
wget https://github.com/rockchip-linux/rknpu2/raw/v1.5.2/runtime/RK3588/Linux/librknn_api/aarch64/librknnrt.so
sudo cp librknnrt.so /usr/lib/
sudo ldconfig
```

## Установка rknn_server (опционально)

**Не нужен для sherpa-onnx** — она линкуется с `librknnrt.so` напрямую. Но может пригодиться для других RKNN-приложений.

```bash
wget https://github.com/rockchip-linux/rknpu2/raw/v1.5.2/runtime/RK3588/Linux/rknn_server/aarch64/usr/bin/rknn_server
sudo cp rknn_server /usr/bin/
sudo chmod +x /usr/bin/rknn_server

# Проверка версии
strings /usr/bin/rknn_server | grep 'build@'
# Должно быть 1.5.2, не 2.x

sudo rknn_server &
```

## Права доступа

Устройство NPU принадлежит группе `render`:

```bash
ls -la /dev/dri/renderD129
# crw-rw---- 1 root render 226, 129 ...

sudo usermod -aG render,video $USER
# После этого — выйти и зайти заново
```

## Совместимость версий

| Драйвер | librknnrt | Что работает |
|---|---|---|
| 0.9.2 | 1.5.2 / 1.6.0 | sherpa-onnx RKNN, RKNN-Toolkit2 v1.6 |
| 0.9.2 | 2.x | ❌ несовместимо |
| 0.9.6+ | 2.x | требует пересборки ядра |

## Грабли

### can't request region for resource
Драйвер встроен в ядро (`=y`), нельзя заменить `.ko`. Обновление NPU = пересборка ядра.

### unsupport Log op
Некоторые RKNN-модели используют операцию `Log`, которую драйвер 0.9.2 не поддерживает. Пример: `silero-vad-v4-rk3588.rknn`. Решение: VAD на CPU.

### rknn_init failed
- Несовместимые версии драйвер ↔ librknnrt
- Модель сконвертирована под другую версию runtime

## Что НЕ работает на NPU

- **Silero VAD** (RKNN-версия) — из-за `Log op`
- **GigaAM v3** — нет RKNN-версии (официальной)

## Что работает на NPU

- sherpa-onnx ASR/TTS модели, если они сконвертированы в `.rknn` под 1.5.2

## Источники

- `~/rknn-backup-1.5.2/` — резервные копии `librknnrt.so` и `rknn_server`
- `~/rknn-toolkit2/rknpu2/runtime/Linux/librknn_api/include/rknn_api.h` — заголовки для сборки
