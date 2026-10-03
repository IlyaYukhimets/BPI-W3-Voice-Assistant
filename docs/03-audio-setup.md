# Настройка звука

## Выход: ES8316 (3.5 мм JACK)

Кодек ES8316 по умолчанию **выключен**. Чтобы включить выход на наушники:

```bash
amixer -c 3 sset 'Headphone' 3
amixer -c 3 sset 'DAC' 192
amixer -c 3 sset 'Left Headphone Mixer Left DAC' on
amixer -c 3 sset 'Right Headphone Mixer Right DAC' on
```

Важно: команда `amixer -c 3 sset 'Speaker' off` **не работает** — в вашей сборке
такого регулятора нет. Используйте только перечисленные выше.

Автоматизация через systemd — см. `scripts/audio-fix.service`.

## Вход: USB-микрофон

Определяется как `card 5: Device [USB2.0 Device]`.

Регуляторы только базовые, AGC/gain/boost отсутствуют:

```bash
amixer -c 5 sset 'Mic' 100% unmute
```

## Диагностика

```bash
arecord -l              # список устройств захвата
aplay -l                # список устройств воспроизведения
soxi file.wav           # параметры WAV
sox file.wav -n stat    # статистика (RMS, амплитуда, DC offset)
```

## Ключевые признаки проблем

| Метрика | Норма | Проблема |
|---|---|---|
| RMS amplitude | > 0.02 | < 0.01 → сигнал тихий |
| Midline amplitude | < 0.05 | > 0.1 → DC offset |
| Rough frequency | 500–3000 | < 200 → гул/шум |
| Maximum amplitude | 0.3–0.9 | < 0.1 → сигнал слишком слабый |

## Спектрограмма

Быстрый визуальный анализ:

```bash
sox file.wav -n spectrogram -o spec.png
```

На спектрограмме **правильной записи речи**:
- Видна «ёлочка» гармоник в диапазоне 0–8 кГц
- Между фразами — тёмные участки (паузы)
- Нет яркой линии на DC (низ спектрограммы)

На **проблемной записи**:
- Сплошное яркое поле (шум)
- Речь едва видна на 1–2 кГц
- Яркая полоса на DC внизу
