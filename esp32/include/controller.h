/**
 * @file controller.h
 * @brief Логика устройства: разбор команд, конечный автомат, Homing, телеметрия.
 *
 * Перенос проверенного поведения из `desktop_app/servo_configurator/simulation/
 * firmware.py`. Симулятор и прошивка реализуют один и тот же конечный автомат и
 * один и тот же протокол, поэтому приложение не отличает их друг от друга.
 *
 * Весь цикл неблокирующий: ни Homing, ни ожидание достижения позиции не
 * останавливают обработку команд. Иначе команда `stop` во время Homing не была
 * бы принята — а именно она обязана исполняться в любом состоянии.
 */

#pragma once

#include <ArduinoJson.h>
#include <Arduino.h>

#include "servo_bus.h"
#include "settings.h"

/// Состояния устройства (раздел 8 `docs/PROTOCOL.md`).
enum class DeviceState : uint8_t {
    Idle,
    Homing,
    Position,
    Motor,
    Fault,
    /// Аварийный останов: движение запрещено до явного снятия оператором.
    EStop,
};

/// Исход процедуры Homing.
enum class HomingResult : uint8_t { Completed, Timeout, Aborted, Error };

/**
 * @brief Управление приводом и обмен с ПК.
 */
class Controller {
   public:
    /// Инициализирует шину, читает конфигурацию, отправляет событие `boot`.
    void begin();

    /// Обрабатывает одну строку команды от ПК.
    void handleLine(const char *line, size_t length);

    /**
     * @brief Периодическая работа: опрос привода, Homing, защиты, телеметрия.
     *
     * Вызывается из главного цикла как можно чаще; внутри всё привязано к
     * системному времени, а не к числу вызовов.
     */
    void tick();

   private:
    // --- обработчики команд ---
    void commandPing(uint32_t id);
    void commandGetConfig(uint32_t id);
    void commandSetConfig(uint32_t id, JsonObjectConst args);
    void commandSaveConfig(uint32_t id);
    void commandRestoreDefaults(uint32_t id, JsonObjectConst args);
    void commandTelemetry(uint32_t id, JsonObjectConst args);
    void commandHomeStart(uint32_t id);
    void commandHomeAbort(uint32_t id);
    void commandMoveTo(uint32_t id, JsonObjectConst args);
    void commandMotorRun(uint32_t id, JsonObjectConst args);
    void commandStop(uint32_t id, JsonObjectConst args);
    void commandReset(uint32_t id);

    // --- периодические проверки ---
    void pollFeedback();
    void advanceHoming();
    void checkPositionReached();
    void checkOverload();
    void checkLinkWatchdog();
    void emitTelemetry();

    // --- вспомогательное ---
    bool stateAllows(const char *command, const char **reason) const;
    void startHoming();
    void finishHoming(HomingResult result, const char *error, const char *message,
                      DeviceState nextState = DeviceState::Idle);
    void changeState(DeviceState next);
    void stopMotion(bool releaseTorque);

    /// Пересчёт показания энкодера в координату с учётом нуля, назначенного Homing.
    int32_t toLogical(int16_t raw) const { return static_cast<int32_t>(raw) + positionOffset_; }

    void sendSuccess(uint32_t id, JsonDocument &data);
    void sendSuccess(uint32_t id);
    void sendFailure(uint32_t id, const char *error, const char *message);
    void sendEvent(const char *name, JsonDocument &data);
    void sendDocument(JsonDocument &document);

    static const char *stateName(DeviceState state);

    ServoBus bus_;
    Settings settings_;
    SettingsStore store_;

    bool configLoaded_ = false;
    bool dirty_ = false;

    DeviceState state_ = DeviceState::Idle;
    bool homed_ = false;
    const char *error_ = nullptr;

    bool telemetryEnabled_ = false;
    uint32_t telemetryPeriodMs_ = 50;
    uint32_t lastTelemetryMs_ = 0;
    uint16_t sequence_ = 0;

    uint32_t lastRxMs_ = 0;
    uint32_t lastPollMs_ = 0;

    /// Последние показания привода и признак их достоверности.
    ServoFeedback feedback_;
    bool feedbackValid_ = false;
    uint32_t consecutiveReadFailures_ = 0;

    /// Смещение между показанием энкодера и координатой после Homing.
    int32_t positionOffset_ = 0;

    int32_t targetPosition_ = 0;
    uint32_t overloadSinceMs_ = 0;

    uint32_t homingStartedMs_ = 0;
    int32_t homingStartPosition_ = 0;
};
