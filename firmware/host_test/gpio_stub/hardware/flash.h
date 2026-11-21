#include <stdint.h>
#define FLASH_DEVINFO_SIZE_8M 8u
void flash_devinfo_set_cs_size(uint32_t cs, uint32_t size);
void flash_devinfo_set_cs_gpio(uint32_t cs, uint32_t gpio);
