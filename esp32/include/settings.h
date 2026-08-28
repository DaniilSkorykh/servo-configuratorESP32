/**
 * @file settings.h
 * @brief Конфигурация устройства и её хранение в NVS (Preferences).
 *
 * Состав параметров и их диапазоны повторяют раздел 7 `docs/PROTOCOL.md`
 * и файл `desktop_app/servo_configurator/protocol/schema.py`. Проверка
 * выполняется здесь независимо от приложения: устройство не должно полагаться
 * на то, что команду прислал именно наш конфигуратор.
 */

#pragma once

#include <ArduinoJson.h>
#include <Arduino.h>

/// Версия схемы конфигурации; несовпадение приводит к загрузке значений по умолчанию.
static constexpr uint8_t CONFIG_VERSION = 1;

/// Направление поиска упора.
enum class HomingDirection : uint8_t { CW = 0, CCW = 1 };

/// Параметры процедуры Homing.
struct HomingSettings {
    HomingDirection direction = HomingDirection::CW;
    uint16_t speed = 300;           ///< шаг/с, 1…3000
    uint16_t loadThreshold = 450;   ///< 0.1 %, 50…1000
    uint32_t timeoutMs = 10000;     ///< мс, 1000…60000
    uint16_t maxTravel = 4096;      ///< шаг, 100…8192
    uint16_t zeroPosition = 0;      ///< шаг, 0…4095
};

/// Параметры штатной работы.
struct OperatingSettings {
    uint16_t speed = 1000;      ///< шаг/с, 1…3000
    uint16_t loadLimit = 600;   ///< 0.1 %, 50…1000
    uint16_t posMin = 0;        ///< шаг, 0…4095
    uint16_t posMax = 4095;     ///< шаг, 0…4095
    uint8_t acceleration = 50;  ///< 0…255, 0 — без ограничения
};

/// Параметры безопасности.
struct SafetySettings {
    uint32_t linkTimeoutMs = 1000;  ///< мс, 0…10000; 0 отключает watchdog связи
};

/**
 * @brief Полная конфигурация устройства.
 */
struct Settings {
    uint8_t servoId = 1;  ///< адрес привода на шине, 0…253
    HomingSettings homing;
    OperatingSettings operating;
    SafetySettings safety;

    /// Возвращает значения по умолчанию.
    static Settings defaults() { return Settings{}; }

    /**
     * @brief Проверяет конфигурацию целиком.
     * @param error Буфер для сообщения об ошибке.
     * @param errorSize Размер буфера.
     * @return true, если все значения и их сочетания допустимы.
     *
     * Межполевые ограничения (раздел 7 протокола) проверяются вместе с
     * диапазонами: рабочий диапазон должен быть непустым, ноль Homing —
     * лежать внутри него, а порог нагрузки — не превышать предел.
     */
    bool validate(char *error, size_t errorSize) const;

    /// Заполняет объект JSON текущими значениями.
    void toJson(JsonObject target) const;

    /**
     * @brief Применяет частичное обновление из JSON.
     *
     * Изменения вносятся в копию, и та проверяется целиком: при отказе
     * исходная конфигурация остаётся нетронутой (атомарность `set_config`).
     *
     * @return true, если обновление принято.
     */
    bool applyPatch(JsonObjectConst patch, char *error, size_t errorSize);
};

/**
 * @brief Энергонезависимое хранилище конфигурации.
 *
 * Конфигурация переживает перезагрузку контроллера — требование п. 4.2
 * задания и пункта 17 сценария приёмки.
 */
class SettingsStore {
   public:
    /**
     * @brief Читает конфигурацию из NVS.
     * @param settings Куда записать прочитанное.
     * @return true, если в памяти была совместимая и корректная конфигурация.
     *
     * Отсутствие записи, чужая версия схемы и не прошедшие проверку значения
     * обрабатываются одинаково: устройство поднимается на значениях по
     * умолчанию, а не отказывается стартовать.
     */
    bool load(Settings &settings);

    /// Записывает конфигурацию в NVS.
    bool save(const Settings &settings);

    /// Стирает сохранённую конфигурацию.
    void clear();
};
