/**
 * @file board_config.h
 * @brief Аппаратная конфигурация платы Waveshare Servo Driver with ESP32.
 *
 * Значения взяты из официальной документации Waveshare (раздел ESP32 Pin
 * Function). Собраны в одном месте: при переходе на другую ревизию платы
 * правится только этот файл.
 */

#pragma once

#include <Arduino.h>

/// Скорость обмена с ПК (USB Type-C → UART0).
static constexpr uint32_t HOST_BAUDRATE = 115200;

/// Скорость шины сервопривода. Заводское значение STS3215 — 1 000 000 бод.
static constexpr uint32_t SERVO_BAUDRATE = 1000000;

/// Аппаратный UART, к которому подключена шина сервоприводов.
static constexpr int SERVO_UART_NUM = 1;

/// Вывод приёма данных от сервопривода (GPIO 18 по документации Waveshare).
static constexpr int SERVO_RX_PIN = 18;

/// Вывод передачи данных сервоприводу (GPIO 19).
static constexpr int SERVO_TX_PIN = 19;

/**
 * @brief Вывод управления направлением полудуплексной шины.
 *
 * На плате Waveshare направление переключается аппаратно, поэтому отдельный
 * сигнал не нужен. Значение -1 отключает управление; если на другой плате
 * потребуется DE/RE, достаточно указать здесь номер вывода — драйвер шины
 * учитывает эту настройку.
 */
static constexpr int SERVO_DIRECTION_PIN = -1;

/// Встроенный светодиод: мигает, показывая, что цикл прошивки жив.
static constexpr int STATUS_LED_PIN = 23;

/// Период мигания светодиода, мс.
static constexpr uint32_t STATUS_LED_PERIOD_MS = 1000;
