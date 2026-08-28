/**
 * @file servo_bus.cpp
 * @brief Реализация обмена по протоколу Feetech ST-серии.
 *
 * Формат пакета:
 *
 *     0xFF 0xFF  ID  LEN  INSTRUCTION  PARAM…  CHECKSUM
 *
 * где `LEN` — число параметров плюс два, а `CHECKSUM` — побитовое дополнение
 * суммы всех байтов, начиная с `ID`. Ответ имеет тот же вид, но вместо
 * инструкции содержит байт состояния привода.
 */

#include "servo_bus.h"

namespace {
/// Признак начала пакета.
constexpr uint8_t HEADER_BYTE = 0xFF;

/// Вычисляет контрольную сумму по правилам Feetech.
uint8_t checksum(const uint8_t *data, size_t length) {
    uint32_t sum = 0;
    for (size_t i = 0; i < length; ++i) {
        sum += data[i];
    }
    return static_cast<uint8_t>(~(sum & 0xFF));
}
}  // namespace

void ServoBus::begin(uint32_t baudrate) {
    serial_.begin(baudrate, SERIAL_8N1, SERVO_RX_PIN, SERVO_TX_PIN);
    serial_.setTimeout(RESPONSE_TIMEOUT_MS);

    if (SERVO_DIRECTION_PIN >= 0) {
        pinMode(SERVO_DIRECTION_PIN, OUTPUT);
        digitalWrite(SERVO_DIRECTION_PIN, LOW);
    }
}

// ---------------------------------------------------------------------------
// Публичные операции
// ---------------------------------------------------------------------------

bool ServoBus::ping(uint8_t id) {
    sendPacket(id, sts_inst::PING, nullptr, 0);
    return receivePacket(id, nullptr, 0);
}

bool ServoBus::readFeedback(uint8_t id, ServoFeedback &feedback) {
    uint8_t buffer[sts_reg::FEEDBACK_LENGTH] = {0};
    if (!readRegister(id, sts_reg::PRESENT_POSITION, sts_reg::FEEDBACK_LENGTH, buffer)) {
        return false;
    }

    // Смещения внутри блока считаются от адреса первого прочитанного регистра.
    const auto at = [&](uint8_t address) -> const uint8_t * {
        return buffer + (address - sts_reg::PRESENT_POSITION);
    };

    // Позиция передаётся без знака: это показание энкодера, а не смещение.
    feedback.position = static_cast<int16_t>(toWord(at(sts_reg::PRESENT_POSITION)) & 0x7FFF);
    feedback.speed = toSigned(toWord(at(sts_reg::PRESENT_SPEED)));
    feedback.load = toSigned(toWord(at(sts_reg::PRESENT_LOAD)));
    feedback.voltage = *at(sts_reg::PRESENT_VOLTAGE);
    feedback.temperature = *at(sts_reg::PRESENT_TEMPERATURE);
    feedback.moving = *at(sts_reg::MOVING) != 0;
    feedback.current = toWord(at(sts_reg::PRESENT_CURRENT));
    return true;
}

bool ServoBus::setMode(uint8_t id, ServoMode mode) {
    // Регистр режима лежит в EEPROM, поэтому запись обрамляется снятием и
    // возвратом блокировки: иначе привод её проигнорирует.
    if (!writeByte(id, sts_reg::LOCK, 0)) {
        return false;
    }
    const bool written = writeByte(id, sts_reg::MODE, static_cast<uint8_t>(mode));
    writeByte(id, sts_reg::LOCK, 1);
    return written;
}

bool ServoBus::setTorque(uint8_t id, bool enabled) {
    return writeByte(id, sts_reg::TORQUE_ENABLE, enabled ? 1 : 0);
}

bool ServoBus::setAcceleration(uint8_t id, uint8_t acceleration) {
    return writeByte(id, sts_reg::ACCELERATION, acceleration);
}

bool ServoBus::moveTo(uint8_t id, uint16_t position, uint16_t speed) {
    // Позиция, время и скорость лежат подряд, поэтому уходят одной посылкой:
    // раздельная запись оставляла бы окно, в котором привод уже получил новую
    // цель, но ещё старую скорость.
    uint8_t payload[6];
    fromWord(position, &payload[0]);
    fromWord(0, &payload[2]);  // время хода: 0 — двигаться с заданной скоростью
    fromWord(speed, &payload[4]);
    return writeRegister(id, sts_reg::GOAL_POSITION, payload, sizeof(payload));
}

bool ServoBus::runWheel(uint8_t id, int16_t speed) {
    return writeWord(id, sts_reg::GOAL_SPEED, fromSigned(speed));
}

bool ServoBus::stopWheel(uint8_t id) {
    return writeWord(id, sts_reg::GOAL_SPEED, 0);
}

// ---------------------------------------------------------------------------
// Работа с регистрами
// ---------------------------------------------------------------------------

bool ServoBus::writeRegister(uint8_t id, uint8_t address, const uint8_t *data, uint8_t length) {
    uint8_t parameters[1 + MAX_PAYLOAD];
    if (length > MAX_PAYLOAD) {
        return false;
    }

    parameters[0] = address;
    memcpy(&parameters[1], data, length);

    sendPacket(id, sts_inst::WRITE, parameters, static_cast<uint8_t>(length + 1));
    return receivePacket(id, nullptr, 0);
}

bool ServoBus::writeByte(uint8_t id, uint8_t address, uint8_t value) {
    return writeRegister(id, address, &value, 1);
}

bool ServoBus::writeWord(uint8_t id, uint8_t address, uint16_t value) {
    uint8_t payload[2];
    fromWord(value, payload);
    return writeRegister(id, address, payload, sizeof(payload));
}

bool ServoBus::readRegister(uint8_t id, uint8_t address, uint8_t length, uint8_t *destination) {
    if (length > MAX_PAYLOAD) {
        return false;
    }

    const uint8_t parameters[2] = {address, length};
    sendPacket(id, sts_inst::READ, parameters, sizeof(parameters));
    return receivePacket(id, destination, length);
}

// ---------------------------------------------------------------------------
// Уровень пакетов
// ---------------------------------------------------------------------------

void ServoBus::sendPacket(uint8_t id, uint8_t instruction, const uint8_t *parameters,
                          uint8_t count) {
    // Остатки предыдущего обмена в приёмном буфере приняли бы за начало ответа.
    flushInput();

    uint8_t frame[6 + MAX_PAYLOAD];
    frame[0] = HEADER_BYTE;
    frame[1] = HEADER_BYTE;
    frame[2] = id;
    frame[3] = static_cast<uint8_t>(count + 2);
    frame[4] = instruction;
    if (count > 0 && parameters != nullptr) {
        memcpy(&frame[5], parameters, count);
    }

    const size_t body = static_cast<size_t>(count) + 3;  // ID, LEN, INSTRUCTION и параметры
    frame[5 + count] = checksum(&frame[2], body);

    beginTransmission();
    serial_.write(frame, 6 + count);
    // Передача должна завершиться до переключения направления, иначе конец
    // посылки будет обрезан.
    serial_.flush();
    endTransmission();
}

bool ServoBus::receivePacket(uint8_t id, uint8_t *payload, uint8_t expected) {
    // Ответ: 0xFF 0xFF ID LEN ERROR [payload] CHECKSUM
    const size_t total = 6u + expected;
    uint8_t frame[6 + MAX_PAYLOAD];

    const size_t received = serial_.readBytes(frame, total);
    if (received != total) {
        ++timeouts_;
        return false;
    }

    if (frame[0] != HEADER_BYTE || frame[1] != HEADER_BYTE || frame[2] != id) {
        return false;
    }

    const size_t body = static_cast<size_t>(expected) + 3;
    if (frame[total - 1] != checksum(&frame[2], body)) {
        return false;
    }

    // frame[4] — байт состояния привода: ненулевое значение означает перегрев,
    // перегрузку или иную зафиксированную им ошибку.
    if (frame[4] != 0) {
        return false;
    }

    if (payload != nullptr && expected > 0) {
        memcpy(payload, &frame[5], expected);
    }
    return true;
}

void ServoBus::beginTransmission() {
    if (SERVO_DIRECTION_PIN >= 0) {
        digitalWrite(SERVO_DIRECTION_PIN, HIGH);
    }
}

void ServoBus::endTransmission() {
    if (SERVO_DIRECTION_PIN >= 0) {
        digitalWrite(SERVO_DIRECTION_PIN, LOW);
    }
}

void ServoBus::flushInput() {
    while (serial_.available() > 0) {
        serial_.read();
    }
}
