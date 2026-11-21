// RP2040-Zero / Arduino-Pico (Earle Philhower). Dn means GPIO n.
// GPIO0 = STPG RUN, GPIO1 = LA RUN, GPIO2 = current bench FPGA enable.
// All pins boot Hi-Z without pulls; no implicit reset/enable operation.
// Legacy 0l/0z/0h/0n .. 2*, s/S and ? remain supported.
// s reports only GPIO0..2 for old three-channel clients; a reports all pins.
// Extended manual examples: 3l, 8z, 14h, 15n, 26z.
// New host: i <16 lowercase hex chars>, then @<token>:<seq> <gpio><l|z|h>.
// ACKs report requested SOFTWARE state, not voltage or glitch-free operation.
#include <Arduino.h>
#include <hardware/gpio.h>
#include <ctype.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

constexpr unsigned NUM_PINS = 12;
const uint targetPins[NUM_PINS] = {0, 1, 2, 3, 4, 5, 6, 7, 8, 14, 15, 26};
enum PinState { STATE_LOW, STATE_HIZ, STATE_HIGH };
PinState pinStates[NUM_PINS];
constexpr unsigned MAX_LINE = 64;
char inputLine[MAX_LINE + 1];
unsigned inputLength = 0;
bool discardLine = false;
char sessionToken[17] = "";
uint32_t lastSequence = 0;

int pinIndex(unsigned pin) {
  for (unsigned i = 0; i < NUM_PINS; ++i) if (targetPins[i] == pin) return i;
  return -1;
}
const char* stateName(PinState state) {
  return state == STATE_LOW ? "LOW" : state == STATE_HIGH ? "HIGH" : "Hi-Z";
}
void applyState(unsigned index, PinState state) {
  const uint pin = targetPins[index];
  if (state == STATE_HIZ) {
    gpio_set_dir(pin, GPIO_IN);
    gpio_disable_pulls(pin);
  } else {
    gpio_disable_pulls(pin);
    gpio_put(pin, state == STATE_HIGH ? 1 : 0); // latch BEFORE output enable
    gpio_set_dir(pin, GPIO_OUT);
  }
  pinStates[index] = state;
}
void printAck(unsigned index) {
  Serial.print("CH"); Serial.print(targetPins[index]); Serial.print(" = ");
  Serial.println(stateName(pinStates[index]));
}
void printOneStatus(unsigned index) {
  Serial.print("CH"); Serial.print(targetPins[index]); Serial.print(" GPIO");
  Serial.print(targetPins[index]); Serial.print(" state=");
  Serial.println(stateName(pinStates[index]));
}
void printStatus(bool extended = false) {
  Serial.println("Status:");
  for (unsigned i = 0; i < (extended ? NUM_PINS : 3U); ++i) printOneStatus(i);
  if (extended) Serial.println("END");
}
void printHelp() {
  Serial.println(); Serial.println("Commands:");
  const char* operations[] = {"l", "z", "h", "n"};
  const char* names[] = {" LOW", " Hi-Z", " HIGH", " next state"};
  for (unsigned i = 0; i < 3; ++i) {
    for (unsigned j = 0; j < 4; ++j) {
      Serial.print("  "); Serial.print(i); Serial.print(operations[j]);
      Serial.print(" : CH"); Serial.print(i); Serial.println(names[j]);
    }
    Serial.println();
  }
  Serial.println("  s  : show status"); Serial.println("  ?  : help");
  Serial.println("  Extended GPIO: 3,4,5,6,7,8,14,15,26 (same l/z/h/n commands)");
  Serial.println("  a  : all supported GPIO status");
  Serial.println("  i TOKEN : host session (16 lowercase hex chars)"); Serial.println();
}
bool validToken(const char* token) {
  if (strlen(token) != 16) return false;
  for (unsigned i = 0; i < 16; ++i)
    if (!((token[i] >= '0' && token[i] <= '9') || (token[i] >= 'a' && token[i] <= 'f'))) return false;
  return true;
}
void identity(const char* token) {
  strcpy(sessionToken, token); lastSequence = 0;
  Serial.print("STPG_GPIO_V1 "); Serial.print(token); Serial.print(" GPIO=");
  for (unsigned i = 0; i < NUM_PINS; ++i) {
    if (i) Serial.print(",");
    Serial.print(targetPins[i]);
  }
  Serial.println();
}
bool parsePinOperation(const char* cmd, unsigned& index, char& operation) {
  if (!isdigit(static_cast<unsigned char>(*cmd)) ||
      (cmd[0] == '0' && isdigit(static_cast<unsigned char>(cmd[1])))) return false;
  char* end = nullptr;
  const unsigned long pin = strtoul(cmd, &end, 10);
  if (end == cmd || !*end || end[1] != '\0' || pin > 26) return false;
  const int found = pinIndex(static_cast<unsigned>(pin));
  if (found < 0) return false;
  operation = static_cast<char>(tolower(static_cast<unsigned char>(*end)));
  if (operation != 'l' && operation != 'z' && operation != 'h' && operation != 'n') return false;
  index = static_cast<unsigned>(found);
  return true;
}
void operate(unsigned index, char operation) {
  PinState state = STATE_HIZ;
  if (operation == 'n')
    state = pinStates[index] == STATE_LOW ? STATE_HIZ : pinStates[index] == STATE_HIZ ? STATE_HIGH : STATE_LOW;
  else if (operation == 'l') state = STATE_LOW;
  else if (operation == 'h') state = STATE_HIGH;
  applyState(index, state);
  printAck(index);
}
void processCommand(char* cmd) {
  while (*cmd && isspace(static_cast<unsigned char>(*cmd))) ++cmd;
  unsigned len = strlen(cmd);
  while (len && isspace(static_cast<unsigned char>(cmd[len - 1]))) cmd[--len] = '\0';
  if (!len) return;
  if (strncmp(cmd, "i ", 2) == 0) {
    if (validToken(cmd + 2)) identity(cmd + 2);
    else Serial.println("ERROR invalid session token");
    return;
  }
  if (*cmd == '@') {
    char* separator = strchr(cmd, ':');
    char* space = strchr(cmd, ' ');
    if (!separator || !space || separator - cmd != 17 || space <= separator + 1) {
      Serial.println("ERROR malformed tagged command"); return;
    }
    *separator = '\0'; *space = '\0';
    char* sequenceEnd = nullptr;
    const unsigned long sequence = strtoul(separator + 1, &sequenceEnd, 10);
    if (!*sessionToken || strcmp(cmd + 1, sessionToken) != 0 ||
        !isdigit(static_cast<unsigned char>(separator[1])) || *sequenceEnd ||
        sequence == 0 || sequence > UINT32_MAX || lastSequence == UINT32_MAX ||
        sequence != lastSequence + 1 || (separator[1] == '0' && separator[2])) {
      Serial.println("ERROR stale or invalid session sequence"); return;
    }
    unsigned index = 0; char operation = 0;
    if (!parsePinOperation(space + 1, index, operation) || operation == 'n') {
      Serial.println("ERROR invalid tagged pin operation"); return;
    }
    lastSequence = static_cast<uint32_t>(sequence);
    Serial.print("@"); Serial.print(sessionToken); Serial.print(":"); Serial.print(lastSequence); Serial.print(" ");
    operate(index, operation);
    return;
  }
  sessionToken[0] = '\0'; // manual/legacy access invalidates tagged ownership
  if (strcmp(cmd, "s") == 0 || strcmp(cmd, "S") == 0) { printStatus(); return; }
  if (strcmp(cmd, "a") == 0 || strcmp(cmd, "A") == 0) { printStatus(true); return; }
  if (strcmp(cmd, "?") == 0) { printHelp(); return; }
  unsigned index = 0; char operation = 0;
  if (!parsePinOperation(cmd, index, operation)) { Serial.println("Invalid command"); return; }
  operate(index, operation);
}
void setup() {
  Serial.begin(115200);
  for (unsigned i = 0; i < NUM_PINS; ++i) {
    gpio_init(targetPins[i]); gpio_set_dir(targetPins[i], GPIO_IN); gpio_disable_pulls(targetPins[i]);
    pinStates[i] = STATE_HIZ;
  }
  delay(1000); // retained startup interval; no indefinite USB-host wait
  Serial.println(); Serial.println("RP2040 Zero multi GPIO 3-state controller");
  printHelp(); printStatus();
}
void loop() {
  while (Serial.available() > 0) {
    const char ch = static_cast<char>(Serial.read());
    if (ch == '\r' || ch == '\n') {
      if (!discardLine && inputLength) {
        inputLine[inputLength] = '\0'; processCommand(inputLine);
      }
      inputLength = 0; discardLine = false;
    } else if (!discardLine) {
      if ((static_cast<unsigned char>(ch) < 32 && ch != '\t') || static_cast<unsigned char>(ch) > 126) {
        Serial.println("Invalid input byte"); inputLength = 0; discardLine = true;
      } else if (inputLength == MAX_LINE) {
        Serial.println("Input too long"); inputLength = 0; discardLine = true;
      } else inputLine[inputLength++] = ch;
    }
  }
}
