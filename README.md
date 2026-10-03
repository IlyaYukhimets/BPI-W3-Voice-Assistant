# BPI-W3 Voice Assistant

Локальный голосовой ассистент для Home Assistant на Banana Pi BPI-W3 (RK3588).

## Что умеет

- 🎤 Запись с USB-микрофона (48 кГц)
- 🧹 Очистка сигнала через sox (highpass/lowpass + нормализация)
- 🧠 Распознавание русской речи (GigaAM v3, ONNX, int8)
- ⚡ NPU RK3588 доступен (драйвер 0.9.2, librknnrt 1.5.2)
- 🔊 Вывод звука через ES8316 (3.5 мм JACK)
- 🗣 Голосовые ответы (Silero v5_5_ru, 5 голосов, RTF 0.13–0.18)

## Стек

| Компонент | Технология |
|---|---|
| Плата | Banana Pi BPI-W3 (RK3588, 8 ГБ) |
| ОС | Ubuntu 22.04.3 LTS (ядро 5.10.160-rockchip) |
| Аудио выход | ES8316 (card 3) |
| Аудио вход | USB-микрофон (card 5) |
| VAD | Silero VAD v4 (CPU) |
| ASR | GigaAM v3 (ONNX int8) |
| Фреймворк | sherpa-onnx v1.13.8 (собран из исходников) |
| TTS | Silero v5_5_ru (torch CPU) |
| NPU | RKNPU 0.9.2 + librknnrt 1.5.2 |

## Документация

1. [Железо](docs/01-hardware.md)
2. [Установка ОС](docs/02-os-install.md)
3. [Настройка звука](docs/03-audio-setup.md)
4. [Настройка NPU](docs/04-npu-setup.md)
5. [Сборка sherpa-onnx](docs/05-sherpa-onnx-build.md)
6. [VAD и ASR модели](docs/06-vad-asr-models.md)
7. [Обработка аудио](docs/07-audio-processing.md)
8. [Troubleshooting](docs/08-troubleshooting.md)
9. [Синтез речи (TTS)](docs/09-tts.md)

## Быстрый старт

```bash
# Установка зависимостей
bash scripts/install-deps.sh

# Озвучить текст (синтез → динамик ES8316)
./scripts/say.sh "Привет! Я готов."

# Скрипт ассистента (запись 7 секунд → распознавание)
~/assistant/listen-and-recognize.sh
```

## Проверенные версии

```
Ядро:          5.10.160-rockchip
RKNPU driver:  0.9.2
librknnrt:     1.5.2
sherpa-onnx:   1.13.8 (собран из исходников)
GigaAM:        v3 (encoder.int8.onnx)
GigaAM punct:  v3 + пунктуация (трансдьюсер, 1025 токенов)
Silero TTS:    v5_5_ru (torch 2.14.1+cpu)
Silero VAD:    v4 (CPU)
```

## Лицензия

MIT
