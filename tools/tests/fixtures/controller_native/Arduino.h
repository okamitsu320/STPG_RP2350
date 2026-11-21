#pragma once
#include <cstdint>
#include <iostream>
#include <string>
using uint = unsigned int;
struct SerialStub {
  std::string input;
  unsigned offset = 0;
  void begin(int) {}
  int available() const { return static_cast<int>(input.size() - offset); }
  int read() { return static_cast<unsigned char>(input[offset++]); }
  template<class T> void print(const T& value) { std::cout << value; }
  template<class T> void println(const T& value) { std::cout << value << '\n'; }
  void println() { std::cout << '\n'; }
};
extern SerialStub Serial;
inline void delay(unsigned) {}
