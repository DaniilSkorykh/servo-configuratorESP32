/**
 * @file controller.cpp
 * @brief Реализация логики устройства.
 */

#include "controller.h"

namespace {
/// Версия протокола обмена с ПК.
constexpr int PROTOCOL_VERSION = 1;

constexpr const char *DEVICE_NAME = "ws-servo-esp32";
constexpr const char *SERVO_NAME = "STS3215";

/// Коды ошибок (раздел 5 `docs/PROTOCOL.md`).
constexpr const char *E_PARSE = "E_PARSE";
constexpr const char *E_SCHEMA = "E_SCHEMA";
constexpr const char *E_UNKNOWN_CMD = "E_UNKNOWN_CMD";
constexpr const char *E_RANGE = "E_RANGE";
constexpr const char *E_STATE = "E_STATE";
constexpr const char *E_NOT_HOMED = "E_NOT_HOMED";
constexpr const char *E_SERVO_TIMEOUT = "E_SERVO_TIMEOUT";
constexpr const char *E_SERVO_ERROR = "E_SERVO_ERROR";
constexpr const char *E_TIMEOUT = "E_TIMEOUT";
constexpr const char *E_NVS = "E_NVS";
constexpr const char *E_LINK_TIMEOUT = "E_LINK_TIMEOUT";

/// Период опроса привода, мс. Чаще не имеет смысла: обмен по шине не мгновенный.
constexpr uint32_t POLL_INTERVAL_MS = 20;

/// Пауза после старта Homing, в течение которой нагрузка не анализируется:
/// при трогании привод даёт кратковременный бросок момента.
constexpr uint32_t HOMING_SETTLE_MS = 300;

/// Допуск, при котором целевая позиция считается достигнутой, шаг.
constexpr int32_t POSITION_REACHED_TOLERANCE = 10;

/// Время удержания перегрузки до аварийной остановки, мс.
constexpr uint32_t OVERLOAD_HOLD_MS = 300;

/// Сколько подряд неудачных опросов привода считать потерей связи с ним.
constexpr uint32_t MAX_READ_FAILURES = 5;

/// Границы периода телеметрии, мс.
constexpr uint32_t TELEMETRY_PERIOD_MIN = 20;
constexpr uint32_t TELEMETRY_PERIOD_MAX = 1000;

/// Размер документа JSON для обмена: с запасом к лимиту кадра в 512 байт.
constexpr size_t JSON_CAPACITY = 1024;

/// Знак направления: CW соответствует убыванию счётчика позиции.
int directionSign(HomingDirection direction) {
    return direction == HomingDirection::CW ? -1 : +1;
}
}  // namespace

// ---------------------------------------------------------------------------
// Жизненный цикл
// ---------------------------------------------------------------------------

void Controller::begin() {
    bus_.begin();

    configLoaded_ = store_.load(settings_);
    if (!configLoaded_) {
        settings_ = Settings::defaults();
    }

    // Ускорение и режим задаются сразу: привод мог остаться в непрерывном
    // вращении после предыдущего сеанса или перезагрузки контроллера.
    bus_.setMode(settings_.servoId, ServoMode::Position);
    bus_.setAcceleration(settings_.servoId, settings_.operating.acceleration);
    bus_.setTorque(settings_.servoId, true);

    lastRxMs_ = millis();
    lastPollMs_ = millis();

    JsonDocument data;
    data["fw"] = FIRMWARE_VERSION;
    data["proto"] = PROTOCOL_VERSION;
    data["cfg_loaded"] = configLoaded_;
    sendEvent("boot", data);
}

// ---------------------------------------------------------------------------
// Приём команд
// ---------------------------------------------------------------------------

void Controller::handleLine(const char *line, size_t length) {
    // Отметка приёма обновляется до разбора: даже испорченная строка
    // доказывает, что ПК на связи, и watchdog не должен срабатывать.
    lastRxMs_ = millis();

    JsonDocument document;
    const DeserializationError parseError = deserializeJson(document, line, length);
    if (parseError) {
        JsonDocument data;
        data["err"] = E_PARSE;
        data["msg"] = parseError.c_str();
        sendEvent("error", data);
        return;
    }

    JsonObjectConst message = document.as<JsonObjectConst>();
    if (message.isNull()) {
        JsonDocument data;
        data["err"] = E_SCHEMA;
        data["msg"] = "ожидался объект";
        sendEvent("error", data);
        return;
    }

    JsonVariantConst rawId = message["id"];
    if (!rawId.is<long>()) {
        JsonDocument data;
        data["err"] = E_SCHEMA;
        data["msg"] = "отсутствует или некорректен id";
        sendEvent("error", data);
        return;
    }
    const uint32_t id = static_cast<uint32_t>(rawId.as<long>());

    const char *command = message["cmd"];
    if (command == nullptr) {
        sendFailure(id, E_SCHEMA, "отсутствует cmd");
        return;
    }

    JsonObjectConst args = message["args"];

    const char *reason = nullptr;
    if (!stateAllows(command, &reason)) {
        sendFailure(id, E_STATE, reason);
        return;
    }

    if (strcmp(command, "ping") == 0) {
        commandPing(id);
    } else if (strcmp(command, "get_config") == 0) {
        commandGetConfig(id);
    } else if (strcmp(command, "set_config") == 0) {
        commandSetConfig(id, args);
    } else if (strcmp(command, "save_config") == 0) {
        commandSaveConfig(id);
    } else if (strcmp(command, "restore_defaults") == 0) {
        commandRestoreDefaults(id, args);
    } else if (strcmp(command, "telemetry") == 0) {
        commandTelemetry(id, args);
    } else if (strcmp(command, "home_start") == 0) {
        commandHomeStart(id);
    } else if (strcmp(command, "home_abort") == 0) {
        commandHomeAbort(id);
    } else if (strcmp(command, "move_to") == 0) {
        commandMoveTo(id, args);
    } else if (strcmp(command, "motor_run") == 0) {
        commandMotorRun(id, args);
    } else if (strcmp(command, "stop") == 0) {
        commandStop(id, args);
    } else {
        sendFailure(id, E_UNKNOWN_CMD, command);
    }
}

bool Controller::stateAllows(const char *command, const char **reason) const {
    // Разрешены всегда, включая состояние fault: без них устройство,
    // сообщившее об ошибке, стало бы неуправляемым.
    if (strcmp(command, "ping") == 0 || strcmp(command, "get_config") == 0 ||
        strcmp(command, "telemetry") == 0 || strcmp(command, "stop") == 0) {
        return true;
    }

    if (state_ == DeviceState::Fault) {
        *reason = "устройство в состоянии fault, требуется stop";
        return false;
    }

    if (state_ == DeviceState::Homing && strcmp(command, "home_abort") != 0) {
        *reason = "выполняется Homing";
        return false;
    }

    if (state_ == DeviceState::Motor && strcmp(command, "motor_run") != 0) {
        *reason = "выполняется непрерывное вращение, требуется stop";
        return false;
    }

    if (strcmp(command, "home_start") == 0 && state_ != DeviceState::Idle) {
        *reason = "Homing допустим только из idle";
        return false;
    }

    return true;
}

// ---------------------------------------------------------------------------
// Команды
// ---------------------------------------------------------------------------

void Controller::commandPing(uint32_t id) {
    JsonDocument data;
    data["fw"] = FIRMWARE_VERSION;
    data["proto"] = PROTOCOL_VERSION;
    data["dev"] = DEVICE_NAME;
    data["servo"] = SERVO_NAME;
    data["servo_id"] = settings_.servoId;
    data["uptime_ms"] = millis();
    sendSuccess(id, data);
}

void Controller::commandGetConfig(uint32_t id) {
    JsonDocument data;
    settings_.toJson(data["config"].to<JsonObject>());
    data["dirty"] = dirty_;
    sendSuccess(id, data);
}

void Controller::commandSetConfig(uint32_t id, JsonObjectConst args) {
    JsonObjectConst patch = args["config"];
    if (patch.isNull()) {
        sendFailure(id, E_SCHEMA, "ожидался объект config");
        return;
    }

    char error[160] = {0};
    if (!settings_.applyPatch(patch, error, sizeof(error))) {
        sendFailure(id, E_RANGE, error);
        return;
    }

    dirty_ = true;
    // Ускорение действует немедленно: пользователь ожидает увидеть эффект
    // сразу после записи, не дожидаясь перезагрузки.
    bus_.setAcceleration(settings_.servoId, settings_.operating.acceleration);

    JsonDocument data;
    settings_.toJson(data["config"].to<JsonObject>());
    data["dirty"] = true;
    sendSuccess(id, data);
}

void Controller::commandSaveConfig(uint32_t id) {
    if (!store_.save(settings_)) {
        sendFailure(id, E_NVS, "не удалось записать конфигурацию");
        return;
    }
    dirty_ = false;

    JsonDocument data;
    data["dirty"] = false;
    sendSuccess(id, data);
}

void Controller::commandRestoreDefaults(uint32_t id, JsonObjectConst args) {
    settings_ = Settings::defaults();
    dirty_ = true;

    const bool persist = !args.isNull() && args["save"].as<bool>();
    if (persist) {
        if (!store_.save(settings_)) {
            sendFailure(id, E_NVS, "не удалось записать конфигурацию");
            return;
        }
        dirty_ = false;
    }

    bus_.setAcceleration(settings_.servoId, settings_.operating.acceleration);

    JsonDocument data;
    settings_.toJson(data["config"].to<JsonObject>());
    data["dirty"] = dirty_;
    sendSuccess(id, data);
}

void Controller::commandTelemetry(uint32_t id, JsonObjectConst args) {
    bool enabled = true;
    uint32_t period = telemetryPeriodMs_;

    if (!args.isNull()) {
        JsonVariantConst enabledValue = args["enabled"];
        if (!enabledValue.isNull()) {
            if (!enabledValue.is<bool>()) {
                sendFailure(id, E_SCHEMA, "enabled должен быть булевым");
                return;
            }
            enabled = enabledValue.as<bool>();
        }

        JsonVariantConst periodValue = args["period_ms"];
        if (!periodValue.isNull()) {
            if (!periodValue.is<long>()) {
                sendFailure(id, E_SCHEMA, "period_ms должен быть целым");
                return;
            }
            period = static_cast<uint32_t>(periodValue.as<long>());
            if (period < TELEMETRY_PERIOD_MIN || period > TELEMETRY_PERIOD_MAX) {
                sendFailure(id, E_RANGE, "period_ms вне диапазона [20, 1000]");
                return;
            }
        }
    }

    telemetryEnabled_ = enabled;
    telemetryPeriodMs_ = period;

    JsonDocument data;
    data["enabled"] = enabled;
    data["period_ms"] = period;
    sendSuccess(id, data);
}

void Controller::commandHomeStart(uint32_t id) {
    startHoming();

    JsonDocument data;
    data["state"] = stateName(DeviceState::Homing);
    sendSuccess(id, data);
    changeState(DeviceState::Homing);
}

void Controller::commandHomeAbort(uint32_t id) {
    if (state_ != DeviceState::Homing) {
        sendFailure(id, E_STATE, "Homing не выполняется");
        return;
    }

    JsonDocument data;
    data["state"] = stateName(DeviceState::Idle);
    sendSuccess(id, data);
    finishHoming(HomingResult::Aborted, nullptr, nullptr);
}

void Controller::commandMoveTo(uint32_t id, JsonObjectConst args) {
    if (!homed_) {
        sendFailure(id, E_NOT_HOMED, "перед позиционированием требуется Homing");
        return;
    }

    JsonVariantConst positionValue = args["pos"];
    if (!positionValue.is<long>()) {
        sendFailure(id, E_SCHEMA, "pos должен быть целым");
        return;
    }
    const int32_t position = positionValue.as<long>();

    if (position < settings_.operating.posMin || position > settings_.operating.posMax) {
        char message[96];
        snprintf(message, sizeof(message), "pos %ld вне диапазона [%u, %u]",
                 static_cast<long>(position), settings_.operating.posMin,
                 settings_.operating.posMax);
        sendFailure(id, E_RANGE, message);
        return;
    }

    uint16_t speed = settings_.operating.speed;
    JsonVariantConst speedValue = args["speed"];
    if (!speedValue.isNull()) {
        const long requested = speedValue.as<long>();
        if (requested <= 0 || requested > 3000) {
            sendFailure(id, E_RANGE, "speed должен быть положительным и не более 3000");
            return;
        }
        speed = static_cast<uint16_t>(requested);
    }

    // Команда задаётся в координатах пользователя, а привод принимает
    // показание энкодера: смещение, назначенное Homing, снимается обратно.
    const int32_t raw = position - positionOffset_;
    if (raw < 0 || raw > 4095) {
        sendFailure(id, E_RANGE, "цель вне диапазона энкодера после калибровки");
        return;
    }

    if (!bus_.setMode(settings_.servoId, ServoMode::Position) ||
        !bus_.moveTo(settings_.servoId, static_cast<uint16_t>(raw), speed)) {
        sendFailure(id, E_SERVO_TIMEOUT, "привод не принял команду");
        return;
    }

    targetPosition_ = position;
    overloadSinceMs_ = 0;

    JsonDocument data;
    data["target"] = position;
    sendSuccess(id, data);
    changeState(DeviceState::Position);
}

void Controller::commandMotorRun(uint32_t id, JsonObjectConst args) {
    const char *direction = args["dir"];
    if (direction == nullptr ||
        (strcmp(direction, "cw") != 0 && strcmp(direction, "ccw") != 0)) {
        sendFailure(id, E_SCHEMA, "dir должен быть cw или ccw");
        return;
    }

    uint16_t speed = settings_.operating.speed;
    JsonVariantConst speedValue = args["speed"];
    if (!speedValue.isNull()) {
        const long requested = speedValue.as<long>();
        if (requested <= 0 || requested > 3000) {
            sendFailure(id, E_RANGE, "speed должен быть положительным и не более 3000");
            return;
        }
        speed = static_cast<uint16_t>(requested);
    }

    const int sign = strcmp(direction, "cw") == 0 ? -1 : +1;
    if (!bus_.setMode(settings_.servoId, ServoMode::Wheel) ||
        !bus_.runWheel(settings_.servoId, static_cast<int16_t>(sign * speed))) {
        sendFailure(id, E_SERVO_TIMEOUT, "привод не принял команду");
        return;
    }

    overloadSinceMs_ = 0;

    JsonDocument data;
    data["dir"] = direction;
    data["speed"] = speed;
    sendSuccess(id, data);
    changeState(DeviceState::Motor);
}

void Controller::commandStop(uint32_t id, JsonObjectConst args) {
    const bool emergency = !args.isNull() && args["emergency"].as<bool>();
    const bool wasHoming = state_ == DeviceState::Homing;

    stopMotion(emergency);
    error_ = nullptr;

    JsonDocument data;
    data["state"] = stateName(DeviceState::Idle);
    sendSuccess(id, data);

    if (wasHoming) {
        // Прерванная процедура обязана сообщить исход, иначе интерфейс
        // останется с индикацией «выполняется».
        finishHoming(HomingResult::Aborted, nullptr, nullptr);
    } else {
        changeState(DeviceState::Idle);
    }
}

// ---------------------------------------------------------------------------
// Периодическая работа
// ---------------------------------------------------------------------------

void Controller::tick() {
    pollFeedback();
    advanceHoming();
    checkPositionReached();
    checkOverload();
    checkLinkWatchdog();
    emitTelemetry();
}

void Controller::pollFeedback() {
    const uint32_t now = millis();
    if (now - lastPollMs_ < POLL_INTERVAL_MS) {
        return;
    }
    lastPollMs_ = now;

    ServoFeedback fresh;
    if (bus_.readFeedback(settings_.servoId, fresh)) {
        feedback_ = fresh;
        feedbackValid_ = true;
        consecutiveReadFailures_ = 0;
        return;
    }

    // Единичный сбой обмена — обычное дело на длинной шине; тревогу поднимает
    // только серия подряд идущих отказов.
    feedbackValid_ = false;
    if (++consecutiveReadFailures_ < MAX_READ_FAILURES) {
        return;
    }

    if (state_ != DeviceState::Fault) {
        stopMotion(false);
        error_ = E_SERVO_TIMEOUT;
        changeState(DeviceState::Fault);

        JsonDocument data;
        data["err"] = E_SERVO_TIMEOUT;
        data["msg"] = "сервопривод не отвечает";
        sendEvent("error", data);
    }
}

void Controller::advanceHoming() {
    if (state_ != DeviceState::Homing) {
        return;
    }

    const uint32_t elapsed = millis() - homingStartedMs_;

    // Порядок проверок задаёт приоритет причин остановки: аварийные раньше
    // штатной, иначе случайный всплеск нагрузки замаскировал бы превышение
    // лимитов.
    if (feedbackValid_) {
        const int32_t travelled = abs(toLogical(feedback_.position) - homingStartPosition_);
        if (travelled > settings_.homing.maxTravel) {
            char message[96];
            snprintf(message, sizeof(message), "пройдено %ld шаг при пределе %u",
                     static_cast<long>(travelled), settings_.homing.maxTravel);
            finishHoming(HomingResult::Error, E_RANGE, message);
            return;
        }
    }

    if (elapsed > settings_.homing.timeoutMs) {
        finishHoming(HomingResult::Timeout, E_TIMEOUT, "упор не найден за отведённое время");
        return;
    }

    if (elapsed < HOMING_SETTLE_MS || !feedbackValid_) {
        return;
    }

    const int32_t load = abs(feedback_.load);
    if (load >= settings_.homing.loadThreshold) {
        stopMotion(false);

        // Упору назначается координата zero_position: смещение подбирается так,
        // чтобы текущее показание энкодера дало именно её.
        positionOffset_ = static_cast<int32_t>(settings_.homing.zeroPosition) - feedback_.position;
        homed_ = true;
        targetPosition_ = settings_.homing.zeroPosition;
        finishHoming(HomingResult::Completed, nullptr, nullptr);
    }
}

void Controller::checkPositionReached() {
    if (state_ != DeviceState::Position || !feedbackValid_) {
        return;
    }

    // Проверяется расстояние до цели, а не только флаг движения: сразу после
    // команды привод ещё стоит, и одного флага хватило бы, чтобы объявить
    // перемещение завершённым, не начав его.
    if (abs(toLogical(feedback_.position) - targetPosition_) > POSITION_REACHED_TOLERANCE) {
        return;
    }
    if (feedback_.moving) {
        return;
    }

    changeState(DeviceState::Idle);
}

void Controller::checkOverload() {
    if ((state_ != DeviceState::Position && state_ != DeviceState::Motor) || !feedbackValid_) {
        overloadSinceMs_ = 0;
        return;
    }

    if (abs(feedback_.load) < settings_.operating.loadLimit) {
        overloadSinceMs_ = 0;
        return;
    }

    const uint32_t now = millis();
    if (overloadSinceMs_ == 0) {
        overloadSinceMs_ = now;
        return;
    }

    // Кратковременные всплески игнорируются: бросок момента при трогании иначе
    // останавливал бы любое движение.
    if (now - overloadSinceMs_ < OVERLOAD_HOLD_MS) {
        return;
    }

    stopMotion(false);
    overloadSinceMs_ = 0;
    error_ = E_SERVO_ERROR;
    changeState(DeviceState::Fault);

    JsonDocument data;
    data["err"] = E_SERVO_ERROR;
    data["msg"] = "нагрузка превышена — вероятно, достигнут механический упор";
    sendEvent("error", data);
}

void Controller::checkLinkWatchdog() {
    const uint32_t timeout = settings_.safety.linkTimeoutMs;
    if (timeout == 0) {
        return;
    }
    if (state_ != DeviceState::Motor && state_ != DeviceState::Position) {
        return;
    }
    if (millis() - lastRxMs_ <= timeout) {
        return;
    }

    // Защита от выдёргивания USB во время движения: без неё привод продолжал бы
    // вращаться, а приложение уже не смогло бы его остановить.
    stopMotion(false);
    error_ = E_LINK_TIMEOUT;
    changeState(DeviceState::Fault);

    JsonDocument data;
    data["err"] = E_LINK_TIMEOUT;
    data["msg"] = "команды от ПК не поступали дольше допустимого, привод остановлен";
    sendEvent("error", data);
}

void Controller::emitTelemetry() {
    if (!telemetryEnabled_) {
        return;
    }

    const uint32_t now = millis();
    if (now - lastTelemetryMs_ < telemetryPeriodMs_) {
        return;
    }
    lastTelemetryMs_ = now;

    JsonDocument data;
    data["seq"] = ++sequence_;
    data["ts"] = now;

    if (feedbackValid_) {
        data["pos"] = toLogical(feedback_.position);
        data["spd"] = feedback_.speed;
        data["load"] = feedback_.load;
        data["volt"] = feedback_.voltage;
        data["temp"] = feedback_.temperature;
        data["cur"] = feedback_.current;
        data["moving"] = feedback_.moving;
    }

    data["state"] = stateName(state_);
    data["homed"] = homed_;
    if (error_ != nullptr) {
        data["err"] = error_;
    } else {
        data["err"] = nullptr;
    }

    sendEvent("tlm", data);
}

// ---------------------------------------------------------------------------
// Homing
// ---------------------------------------------------------------------------

void Controller::startHoming() {
    homingStartedMs_ = millis();
    homingStartPosition_ = feedbackValid_ ? toLogical(feedback_.position) : 0;
    homed_ = false;

    const int sign = directionSign(settings_.homing.direction);
    bus_.setMode(settings_.servoId, ServoMode::Wheel);
    bus_.runWheel(settings_.servoId, static_cast<int16_t>(sign * settings_.homing.speed));
}

void Controller::finishHoming(HomingResult result, const char *error, const char *message) {
    // Привод останавливается при любом исходе, включая ошибочный.
    stopMotion(false);

    JsonDocument data;
    switch (result) {
        case HomingResult::Completed: data["result"] = "completed"; break;
        case HomingResult::Timeout: data["result"] = "timeout"; break;
        case HomingResult::Aborted: data["result"] = "aborted"; break;
        case HomingResult::Error: data["result"] = "error"; break;
    }

    data["pos"] = feedbackValid_ ? toLogical(feedback_.position) : 0;
    data["elapsed_ms"] = millis() - homingStartedMs_;
    if (error != nullptr) {
        data["err"] = error;
        if (message != nullptr) {
            data["msg"] = message;
        }
    }
    sendEvent("homing", data);

    if (result == HomingResult::Completed || result == HomingResult::Aborted) {
        if (result == HomingResult::Completed) {
            error_ = nullptr;
        }
        changeState(DeviceState::Idle);
    } else {
        error_ = error;
        changeState(DeviceState::Fault);
    }
}

// ---------------------------------------------------------------------------
// Состояние и движение
// ---------------------------------------------------------------------------

void Controller::changeState(DeviceState next) {
    if (next == state_) {
        return;
    }

    const DeviceState previous = state_;
    state_ = next;

    JsonDocument data;
    data["state"] = stateName(next);
    data["prev"] = stateName(previous);
    sendEvent("state", data);
}

void Controller::stopMotion(bool releaseTorque) {
    // Сначала снимается скорость непрерывного вращения, затем — при
    // необходимости — момент: обратный порядок оставил бы привод свободным
    // с ещё не обнулённой целью.
    bus_.stopWheel(settings_.servoId);
    bus_.setMode(settings_.servoId, ServoMode::Position);

    if (feedbackValid_) {
        // Удержание текущей позиции: без новой цели привод продолжил бы
        // отрабатывать прежнюю.
        bus_.moveTo(settings_.servoId, static_cast<uint16_t>(feedback_.position),
                    settings_.operating.speed);
        targetPosition_ = toLogical(feedback_.position);
    }

    bus_.setTorque(settings_.servoId, !releaseTorque);
    overloadSinceMs_ = 0;
}

const char *Controller::stateName(DeviceState state) {
    switch (state) {
        case DeviceState::Idle: return "idle";
        case DeviceState::Homing: return "homing";
        case DeviceState::Position: return "position";
        case DeviceState::Motor: return "motor";
        case DeviceState::Fault: return "fault";
    }
    return "fault";
}

// ---------------------------------------------------------------------------
// Отправка сообщений
// ---------------------------------------------------------------------------

void Controller::sendSuccess(uint32_t id, JsonDocument &data) {
    JsonDocument document;
    document["id"] = id;
    document["ok"] = true;
    document["data"] = data;
    sendDocument(document);
}

void Controller::sendSuccess(uint32_t id) {
    JsonDocument empty;
    empty.to<JsonObject>();
    sendSuccess(id, empty);
}

void Controller::sendFailure(uint32_t id, const char *error, const char *message) {
    JsonDocument document;
    document["id"] = id;
    document["ok"] = false;
    document["err"] = error;
    if (message != nullptr) {
        document["msg"] = message;
    }
    sendDocument(document);
}

void Controller::sendEvent(const char *name, JsonDocument &data) {
    JsonDocument document;
    document["evt"] = name;
    document["data"] = data;
    sendDocument(document);
}

void Controller::sendDocument(JsonDocument &document) {
    serializeJson(document, Serial);
    Serial.write('\n');
}
