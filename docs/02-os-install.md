# Установка ОС на BPI-W3

## Выбор ОС

**Ubuntu 22.04.3 LTS (preinstalled-server-arm64-bpi-w3)**
- Проверенный образ, работает с ядром 5.10.160-rockchip
- Скачивается с ресурсов Banana Pi / Armbian

Альтернативы: Armbian (свежее ядро, но менее предсказуемо), Debian.

## Прошивка через rkdeveloptool (Linux-хост)

### 1. Установка утилиты

В CachyOS/Arch:
```bash
paru -S rkdeveloptool-git
```

На Ubuntu/Debian:
```bash
sudo apt install rkdeveloptool
```

### 2. Перевод платы в Maskrom

- Отключить питание и все кабели
- Подключить USB-C ↔ USB-A к ПК
- Зажать кнопку MASKROM
- Подать питание (DC 12V)
- Отпустить через 2 секунды

Проверка:
```bash
sudo rkdeveloptool ld
# Должно появиться: DevNo=1 Vid=0x2207,Pid=0x350b
```

### 3. Загрузка загрузчика

```bash
wget https://dl.radxa.com/rock5/sw/images/loader/rk3588_spl_loader_v1.15.113.bin
sudo rkdeveloptool db rk3588_spl_loader_v1.15.113.bin
```

Успех: `Downloading bootloader succeeded`

### 4. Запись образа

**Путь к файлу не должен содержать кириллицу** — `rkdeveloptool` не переваривает не-ASCII.

```bash
xz -dk ubuntu-22.04.3-preinstalled-server-arm64-bpi-w3.img.xz
sudo rkdeveloptool wl 0 ubuntu-22.04.3-preinstalled-server-arm64-bpi-w3.img
sudo rkdeveloptool rd
```

Запись ~30 ГБ занимает 5–10 минут.

## Первый запуск

- Отключить USB-C (Maskrom)
- Подключить монитор HDMI, клавиатуру, Ethernet
- Подать питание DC 12V/2A — **слабое питание = сбои PCIe и Ethernet**

Логин по умолчанию: `ubuntu` / `ubuntu`

## Устранение проблем

### Плата не определяется в Maskrom
- Другой USB-кабель (обязательно дата-кабель, не только зарядка)
- Попробовать другой USB-порт ПК
- Проверить питание (не только USB-C, но и DC)

### Ошибка «creating comm object failed»
Создать udev-правило:

```bash
echo 'SUBSYSTEMS=="usb", ATTRS{idVendor}=="2207", ATTRS{idProduct}=="350b", MODE="0666", GROUP="users"' | \
  sudo tee /etc/udev/rules.d/99-rkdeveloptool.rules
sudo udevadm control --reload-rules
sudo udevadm trigger
```

### «Loading Loader failed»
Использовать `db` (download boot), а не `ul` (upgrade loader) в режиме Maskrom.

### Плата не загружается после прошивки
Питание. DC 12V/2A стабильно, не USB-C. Слабое питание проявляется как сбои PCIe/Ethernet при загрузке.

## Доступ по UART

Скорость **1500000** (не 115200). Адаптер должен тянуть эту скорость (FT232RL — да, CH340 — не всегда).

```bash
sudo picocom -b 1500000 /dev/ttyUSB0
```

Выход: `Ctrl+A`, `Ctrl+X`.
