/**
 * @file settings.cpp
 * @brief Проверка, сериализация и хранение конфигурации.
 */

#include "settings.h"

#include <Preferences.h>

#include <limits>

namespace {
/// Пространство имён и ключ в NVS.
constexpr const char *NVS_NAMESPACE = "servocfg";
constexpr const char *NVS_KEY_BLOB = "settings";
constexpr const char *NVS_KEY_VERSION = "version";

/// Проверяет попадание значения в диапазон, формируя сообщение при выходе.
template <typename T>
bool inRange(T value, T minimum, T maximum, const char *name, char *error, size_t errorSize) {
    if (value >= minimum && value <= maximum) {
        return true;
    }
    snprintf(error, errorSize, "%s: %ld вне диапазона [%ld, %ld]", name,
             static_cast<long>(value), static_cast<long>(minimum), static_cast<long>(maximum));
    return false;
}

/**
 * @brief Читает целое поле из JSON, если оно присутствует.
 *
 * Значение проверяется на попадание в диапазон целевого типа до приведения:
 * молчаливое усечение опаснее отказа. Число 65836, приведённое к `uint16_t`,
 * превращается в 300 и беспрепятственно проходит последующую проверку
 * диапазона, хотя пользователь прислал совсем другое значение.
 */
template <typename T>
bool readInt(JsonObjectConst source, const char *key, T &target, char *error, size_t errorSize) {
    JsonVariantConst value = source[key];
    if (value.isNull()) {
        return true;
    }
    if (!value.is<long>()) {
        snprintf(error, errorSize, "%s: ожидалось целое число", key);
        return false;
    }

    const long raw = value.as<long>();
    const long minimum = static_cast<long>(std::numeric_limits<T>::min());
    const long maximum = static_cast<long>(std::numeric_limits<T>::max());
    if (raw < minimum || raw > maximum) {
        snprintf(error, errorSize, "%s: %ld вне представимого диапазона [%ld, %ld]", key, raw,
                 minimum, maximum);
        return false;
    }

    target = static_cast<T>(raw);
    return true;
}
}  // namespace

// ---------------------------------------------------------------------------
// Проверка
// ---------------------------------------------------------------------------

bool Settings::validate(char *error, size_t errorSize) const {
    if (!inRange<long>(servoId, 0, 253, "servo.id", error, errorSize)) return false;

    if (!inRange<long>(homing.speed, 1, 3000, "homing.speed", error, errorSize)) return false;
    if (!inRange<long>(homing.loadThreshold, 50, 1000, "homing.load_threshold", error, errorSize))
        return false;
    if (!inRange<long>(homing.timeoutMs, 1000, 60000, "homing.timeout_ms", error, errorSize))
        return false;
    if (!inRange<long>(homing.maxTravel, 100, 8192, "homing.max_travel", error, errorSize))
        return false;
    if (!inRange<long>(homing.zeroPosition, 0, 4095, "homing.zero_position", error, errorSize))
        return false;

    if (!inRange<long>(operating.speed, 1, 3000, "operating.speed", error, errorSize)) return false;
    if (!inRange<long>(operating.loadLimit, 50, 1000, "operating.load_limit", error, errorSize))
        return false;
    if (!inRange<long>(operating.posMin, 0, 4095, "operating.pos_min", error, errorSize))
        return false;
    if (!inRange<long>(operating.posMax, 0, 4095, "operating.pos_max", error, errorSize))
        return false;
    if (!inRange<long>(operating.acceleration, 0, 255, "operating.accel", error, errorSize))
        return false;

    if (!inRange<long>(safety.linkTimeoutMs, 0, 10000, "safety.link_timeout_ms", error, errorSize))
        return false;

    // --- межполевые ограничения ---

    if (operating.posMin >= operating.posMax) {
        snprintf(error, errorSize,
                 "operating.pos_min (%u) должна быть строго меньше operating.pos_max (%u)",
                 operating.posMin, operating.posMax);
        return false;
    }

    if (homing.zeroPosition < operating.posMin || homing.zeroPosition > operating.posMax) {
        snprintf(error, errorSize,
                 "homing.zero_position (%u) должна лежать в рабочем диапазоне [%u, %u]",
                 homing.zeroPosition, operating.posMin, operating.posMax);
        return false;
    }

    if (homing.loadThreshold > operating.loadLimit) {
        snprintf(error, errorSize,
                 "homing.load_threshold (%u) не должен превышать operating.load_limit (%u)",
                 homing.loadThreshold, operating.loadLimit);
        return false;
    }

    return true;
}

// ---------------------------------------------------------------------------
// Сериализация
// ---------------------------------------------------------------------------

void Settings::toJson(JsonObject target) const {
    target["version"] = CONFIG_VERSION;

    JsonObject servo = target["servo"].to<JsonObject>();
    servo["id"] = servoId;

    JsonObject homingNode = target["homing"].to<JsonObject>();
    homingNode["dir"] = homing.direction == HomingDirection::CW ? "cw" : "ccw";
    homingNode["speed"] = homing.speed;
    homingNode["load_threshold"] = homing.loadThreshold;
    homingNode["timeout_ms"] = homing.timeoutMs;
    homingNode["max_travel"] = homing.maxTravel;
    homingNode["zero_position"] = homing.zeroPosition;

    JsonObject operatingNode = target["operating"].to<JsonObject>();
    operatingNode["speed"] = operating.speed;
    operatingNode["load_limit"] = operating.loadLimit;
    operatingNode["pos_min"] = operating.posMin;
    operatingNode["pos_max"] = operating.posMax;
    operatingNode["accel"] = operating.acceleration;

    JsonObject safetyNode = target["safety"].to<JsonObject>();
    safetyNode["link_timeout_ms"] = safety.linkTimeoutMs;
}

bool Settings::applyPatch(JsonObjectConst patch, char *error, size_t errorSize) {
    // Правки вносятся в копию: отвергнутый патч не должен оставить конфигурацию
    // наполовину обновлённой.
    Settings candidate = *this;

    JsonObjectConst servo = patch["servo"];
    if (!servo.isNull()) {
        if (!readInt(servo, "id", candidate.servoId, error, errorSize)) return false;
    }

    JsonObjectConst homingNode = patch["homing"];
    if (!homingNode.isNull()) {
        JsonVariantConst direction = homingNode["dir"];
        if (!direction.isNull()) {
            const char *text = direction.as<const char *>();
            if (text == nullptr) {
                snprintf(error, errorSize, "homing.dir: ожидалась строка");
                return false;
            }
            if (strcmp(text, "cw") == 0) {
                candidate.homing.direction = HomingDirection::CW;
            } else if (strcmp(text, "ccw") == 0) {
                candidate.homing.direction = HomingDirection::CCW;
            } else {
                snprintf(error, errorSize, "homing.dir: ожидалось cw или ccw");
                return false;
            }
        }

        if (!readInt(homingNode, "speed", candidate.homing.speed, error, errorSize)) return false;
        if (!readInt(homingNode, "load_threshold", candidate.homing.loadThreshold, error,
                     errorSize))
            return false;
        if (!readInt(homingNode, "timeout_ms", candidate.homing.timeoutMs, error, errorSize))
            return false;
        if (!readInt(homingNode, "max_travel", candidate.homing.maxTravel, error, errorSize))
            return false;
        if (!readInt(homingNode, "zero_position", candidate.homing.zeroPosition, error, errorSize))
            return false;
    }

    JsonObjectConst operatingNode = patch["operating"];
    if (!operatingNode.isNull()) {
        if (!readInt(operatingNode, "speed", candidate.operating.speed, error, errorSize))
            return false;
        if (!readInt(operatingNode, "load_limit", candidate.operating.loadLimit, error, errorSize))
            return false;
        if (!readInt(operatingNode, "pos_min", candidate.operating.posMin, error, errorSize))
            return false;
        if (!readInt(operatingNode, "pos_max", candidate.operating.posMax, error, errorSize))
            return false;
        if (!readInt(operatingNode, "accel", candidate.operating.acceleration, error, errorSize))
            return false;
    }

    JsonObjectConst safetyNode = patch["safety"];
    if (!safetyNode.isNull()) {
        if (!readInt(safetyNode, "link_timeout_ms", candidate.safety.linkTimeoutMs, error,
                     errorSize))
            return false;
    }

    if (!candidate.validate(error, errorSize)) {
        return false;
    }

    *this = candidate;
    return true;
}

// ---------------------------------------------------------------------------
// Хранилище
// ---------------------------------------------------------------------------

bool SettingsStore::load(Settings &settings) {
    Preferences preferences;
    if (!preferences.begin(NVS_NAMESPACE, /*readOnly=*/true)) {
        return false;
    }

    const uint8_t storedVersion = preferences.getUChar(NVS_KEY_VERSION, 0);
    if (storedVersion != CONFIG_VERSION) {
        // Схема изменилась: прежние байты интерпретировать нельзя.
        preferences.end();
        return false;
    }

    Settings stored;
    const size_t read = preferences.getBytes(NVS_KEY_BLOB, &stored, sizeof(stored));
    preferences.end();

    if (read != sizeof(stored)) {
        return false;
    }

    char error[128];
    if (!stored.validate(error, sizeof(error))) {
        // Сохранённые значения повреждены — работать по ним небезопасно.
        return false;
    }

    settings = stored;
    return true;
}

bool SettingsStore::save(const Settings &settings) {
    Preferences preferences;
    if (!preferences.begin(NVS_NAMESPACE, /*readOnly=*/false)) {
        return false;
    }

    const size_t written = preferences.putBytes(NVS_KEY_BLOB, &settings, sizeof(settings));
    preferences.putUChar(NVS_KEY_VERSION, CONFIG_VERSION);
    preferences.end();

    return written == sizeof(settings);
}

void SettingsStore::clear() {
    Preferences preferences;
    if (preferences.begin(NVS_NAMESPACE, /*readOnly=*/false)) {
        preferences.clear();
        preferences.end();
    }
}
