/**
 * @file main.cpp
 * @brief Точка входа прошивки: приём строк от ПК и главный цикл.
 *
 * Строки собираются побайтно, без блокирующего чтения: пока команда не пришла
 * целиком, контроллер продолжает опрашивать привод и отдавать телеметрию.
 * Блокирующее ожидание строки остановило бы и Homing, и защиты.
 */

#include <Arduino.h>

#include "board_config.h"
#include "controller.h"

namespace {
/// Максимальная длина принимаемой строки (раздел 1 протокола).
constexpr size_t MAX_LINE_LENGTH = 512;

Controller controller;

char lineBuffer[MAX_LINE_LENGTH + 1];
size_t lineLength = 0;

/**
 * @brief Отбрасывать ли остаток текущей строки.
 *
 * Взводится при переполнении буфера: хвост слишком длинной строки не должен
 * быть принят за начало следующей команды. Приём восстанавливается на
 * ближайшем разделителе.
 */
bool skipLine = false;

/// Мигание светодиодом: признак того, что главный цикл не завис.
void updateStatusLed() {
    static uint32_t lastToggleMs = 0;
    static bool state = false;

    const uint32_t now = millis();
    if (now - lastToggleMs < STATUS_LED_PERIOD_MS) {
        return;
    }
    lastToggleMs = now;
    state = !state;
    digitalWrite(STATUS_LED_PIN, state ? HIGH : LOW);
}

/// Обрабатывает накопленную строку и готовит буфер к следующей.
void completeLine() {
    if (skipLine) {
        // Хвост переполненной строки: команда уже испорчена.
        skipLine = false;
        lineLength = 0;
        return;
    }

    if (lineLength > 0) {
        lineBuffer[lineLength] = '\0';
        controller.handleLine(lineBuffer, lineLength);
    }
    lineLength = 0;
}

/// Читает всё, что накопилось в приёмном буфере, не дожидаясь новых байтов.
void readHost() {
    while (Serial.available() > 0) {
        const int value = Serial.read();
        if (value < 0) {
            return;
        }

        const char symbol = static_cast<char>(value);
        if (symbol == '\n') {
            completeLine();
            continue;
        }
        if (symbol == '\r') {
            continue;  // совместимость с мониторами порта
        }

        if (lineLength >= MAX_LINE_LENGTH) {
            skipLine = true;
            lineLength = 0;
            continue;
        }

        lineBuffer[lineLength++] = symbol;
    }
}
}  // namespace

void setup() {
    Serial.begin(HOST_BAUDRATE);

    pinMode(STATUS_LED_PIN, OUTPUT);
    digitalWrite(STATUS_LED_PIN, LOW);

    controller.begin();
}

void loop() {
    readHost();
    controller.tick();
    updateStatusLed();
}
