#include "Arduino.h"
#include <cassert>
SerialStub Serial;
void gpio_init(unsigned pin) { assert(pin < 30); std::cerr << "init " << pin << '\n'; }
void gpio_set_dir(unsigned pin, bool value) { assert(pin < 30); std::cerr << "dir " << pin << ' ' << value << '\n'; }
void gpio_disable_pulls(unsigned pin) { assert(pin < 30); std::cerr << "pulls_off " << pin << '\n'; }
void gpio_put(unsigned pin, bool value) { assert(pin < 30); std::cerr << "put " << pin << ' ' << value << '\n'; }
#include STPG_CONTROLLER_SKETCH
int main() {
  setup();
  std::string line;
  unsigned step = 0;
  while (std::getline(std::cin, line)) {
    std::cerr << "step " << step++ << '\n';
    Serial.input = line + "\n"; Serial.offset = 0;
    loop();
  }
}
