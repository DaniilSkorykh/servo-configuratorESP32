/**
 * @file servo_bus.h
 * @brief Драйвер шины Feetech ST-серии (STS3215).
 *
 * Реализует протокол пакетов Feetech поверх полудуплексного UART. Написан
 * самостоятельно, а не взят из официальной библиотеки SCServo, по двум
 * причинам:
 *
 *  - прошивке нужен небольшой и понятный набор операций (ping, чтение блока
 *    обратной связи, запись цели и режима), а не полный API библиотеки;
 *  - таблица регистров и порядок байтов собраны в одном месте, поэтому
 *    расхождение с конкретной ревизией привода правится точечно.
 *
 * @warning Различие SC- и ST-серий. У ST-серии (STS3215) многобайтовые
 * значения передаются младшим байтом вперёд (little-endian), у SC-серии —
 * старшим. Здесь реализован именно ST-вариант; при работе с SC-приводом
 * порядок байтов нужно изменить в @ref ServoBus::toWord и @ref ServoBus::fromWord.
 */

#pragma once

#include <Arduino.h>

#include "board_config.h"

/// Адреса регистров STS3215 (таблица памяти ST-серии).
namespace sts_reg {
// --- EEPROM ---
static constexpr uint8_t ID = 5;
static constexpr uint8_t BAUD_RATE = 6;
static constexpr uint8_t MIN_ANGLE_LIMIT = 9;   ///< 9 — младший байт, 10 — старший
static constexpr uint8_t MAX_ANGLE_LIMIT = 11;  ///< 11 и 12
static constexpr uint8_t OFFSET = 31;           ///< 31 и 32, калибровка нуля
static constexpr uint8_t MODE = 33;             ///< 0 — позиционный, 1 — непрерывный

// --- SRAM ---
static constexpr uint8_t TORQUE_ENABLE = 40;
static constexpr uint8_t ACCELERATION = 41;
static constexpr uint8_t GOAL_POSITION = 42;  ///< 42 и 43
static constexpr uint8_t GOAL_TIME = 44;      ///< 44 и 45
static constexpr uint8_t GOAL_SPEED = 46;     ///< 46 и 47
static constexpr uint8_t LOCK = 55;           ///< 0 — EEPROM разблокирована

static constexpr uint8_t PRESENT_POSITION = 56;  ///< начало блока обратной связи
static constexpr uint8_t PRESENT_SPEED = 58;
static constexpr uint8_t PRESENT_LOAD = 60;
static constexpr uint8_t PRESENT_VOLTAGE = 62;
static constexpr uint8_t PRESENT_TEMPERATURE = 63;
static constexpr uint8_t MOVING = 66;
static constexpr uint8_t PRESENT_CURRENT = 69;  ///< 69 и 70

/// Длина блока обратной связи от PRESENT_POSITION до PRESENT_CURRENT включительно.
static constexpr uint8_t FEEDBACK_LENGTH = PRESENT_CURRENT + 2 - PRESENT_POSITION;
}  // namespace sts_reg

/// Коды инструкций протокола Feetech.
namespace sts_inst {
static constexpr uint8_t PING = 0x01;
static constexpr uint8_t READ = 0x02;
static constexpr uint8_t WRITE = 0x03;
}  // namespace sts_inst

/// Режим работы привода.
enum class ServoMode : uint8_t {
    Position = 0,  ///< отработка целевой позиции
    Wheel = 1,     ///< непрерывное вращение с заданной скоростью
};

/**
 * @brief Показания привода за один обмен.
 *
 * Все поля читаются одним пакетом: последовательные запросы по регистру
 * заняли бы на порядок больше времени и сбили бы период телеметрии.
 */
struct ServoFeedback {
    int16_t position = 0;     ///< шаг, 0…4095
    int16_t speed = 0;        ///< шаг/с, знак задаёт направление
    int16_t load = 0;         ///< 0.1 %, знак задаёт направление
    uint8_t voltage = 0;      ///< 0.1 В
    uint8_t temperature = 0;  ///< °C
    uint16_t current = 0;     ///< единицы 6.5 мА
    bool moving = false;
};

/**
 * @brief Обмен с сервоприводом по полудуплексной шине.
 */
class ServoBus {
   public:
    /// Инициализирует UART и вывод управления направлением.
    void begin(uint32_t baudrate = SERVO_BAUDRATE);

    /// Проверяет наличие привода на шине.
    bool ping(uint8_t id);

    /// Читает блок обратной связи одним запросом.
    bool readFeedback(uint8_t id, ServoFeedback &feedback);

    /// Переключает режим работы (позиционный или непрерывное вращение).
    bool setMode(uint8_t id, ServoMode mode);

    /// Включает или снимает момент удержания.
    bool setTorque(uint8_t id, bool enabled);

    /// Задаёт ускорение (0 — без ограничения).
    bool setAcceleration(uint8_t id, uint8_t acceleration);

    /**
     * @brief Задаёт целевую позицию и скорость подхода.
     * @param position Целевая позиция в шагах.
     * @param speed Скорость, шаг/с.
     */
    bool moveTo(uint8_t id, uint16_t position, uint16_t speed);

    /**
     * @brief Задаёт скорость непрерывного вращения.
     * @param speed Скорость со знаком: отрицательное значение — вращение
     *        в сторону убывания счётчика позиции.
     */
    bool runWheel(uint8_t id, int16_t speed);

    /// Прекращает вращение в непрерывном режиме.
    bool stopWheel(uint8_t id);

    /// Сколько обменов завершилось без ответа с момента запуска.
    uint32_t timeouts() const { return timeouts_; }

   private:
    /// Таймаут ожидания ответа привода, мс.
    static constexpr uint32_t RESPONSE_TIMEOUT_MS = 20;

    /// Максимальная длина полезной части ответа.
    static constexpr size_t MAX_PAYLOAD = 32;

    bool writeRegister(uint8_t id, uint8_t address, const uint8_t *data, uint8_t length);
    bool writeByte(uint8_t id, uint8_t address, uint8_t value);
    bool writeWord(uint8_t id, uint8_t address, uint16_t value);
    bool readRegister(uint8_t id, uint8_t address, uint8_t length, uint8_t *destination);

    void sendPacket(uint8_t id, uint8_t instruction, const uint8_t *parameters, uint8_t count);
    bool receivePacket(uint8_t id, uint8_t *payload, uint8_t expected);

    void beginTransmission();
    void endTransmission();
    void flushInput();

    /// Собирает 16-битное значение из двух байтов (порядок ST-серии).
    static uint16_t toWord(const uint8_t *data) {
        return static_cast<uint16_t>(data[0]) | (static_cast<uint16_t>(data[1]) << 8);
    }

    /// Раскладывает 16-битное значение по двум байтам (порядок ST-серии).
    static void fromWord(uint16_t value, uint8_t *data) {
        data[0] = static_cast<uint8_t>(value & 0xFF);
        data[1] = static_cast<uint8_t>(value >> 8);
    }

    /**
     * @brief Преобразует значение привода со знаком в бите 15.
     *
     * Скорость и нагрузка передаются как модуль величины, а знак направления
     * задаётся старшим битом, а не дополнительным кодом.
     */
    static int16_t toSigned(uint16_t raw) {
        const int16_t magnitude = static_cast<int16_t>(raw & 0x7FFF);
        return (raw & 0x8000) ? static_cast<int16_t>(-magnitude) : magnitude;
    }

    /// Обратное преобразование: модуль плюс знак в бите 15.
    static uint16_t fromSigned(int16_t value) {
        const uint16_t magnitude = static_cast<uint16_t>(value < 0 ? -value : value) & 0x7FFF;
        return value < 0 ? static_cast<uint16_t>(magnitude | 0x8000) : magnitude;
    }

    HardwareSerial serial_{SERVO_UART_NUM};
    uint32_t timeouts_ = 0;
};
