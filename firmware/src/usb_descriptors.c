#include "pico/unique_id.h"
#include "device/usbd.h"
#include "class/cdc/cdc_device.h"

#define USBD_VID 0x2E8A
#define USBD_PID 0x000A
#define USBD_DESC_LEN (TUD_CONFIG_DESC_LEN + TUD_CDC_DESC_LEN)

#define USBD_ITF_CDC 0
#define USBD_ITF_MAX 2

#define USBD_CDC_EP_CMD 0x81
#define USBD_CDC_EP_OUT 0x02
#define USBD_CDC_EP_IN 0x82
#define USBD_CDC_CMD_MAX_SIZE 8
#define USBD_CDC_IN_OUT_MAX_SIZE 64

enum {
    USBD_STR_LANG = 0,
    USBD_STR_MANUFACTURER,
    USBD_STR_PRODUCT,
    USBD_STR_SERIAL,
    USBD_STR_CDC,
};

static const tusb_desc_device_t usbd_desc_device = {
    .bLength = sizeof(tusb_desc_device_t),
    .bDescriptorType = TUSB_DESC_DEVICE,
    .bcdUSB = 0x0200,
    .bDeviceClass = TUSB_CLASS_MISC,
    .bDeviceSubClass = MISC_SUBCLASS_COMMON,
    .bDeviceProtocol = MISC_PROTOCOL_IAD,
    .bMaxPacketSize0 = CFG_TUD_ENDPOINT0_SIZE,
    .idVendor = USBD_VID,
    .idProduct = USBD_PID,
    .bcdDevice = 0x0100,
    .iManufacturer = USBD_STR_MANUFACTURER,
    .iProduct = USBD_STR_PRODUCT,
    .iSerialNumber = USBD_STR_SERIAL,
    .bNumConfigurations = 1,
};

static const uint8_t usbd_desc_cfg[USBD_DESC_LEN] = {
    TUD_CONFIG_DESCRIPTOR(1, USBD_ITF_MAX, 0, USBD_DESC_LEN, 0, 250),
    TUD_CDC_DESCRIPTOR(USBD_ITF_CDC, USBD_STR_CDC, USBD_CDC_EP_CMD,
                       USBD_CDC_CMD_MAX_SIZE, USBD_CDC_EP_OUT, USBD_CDC_EP_IN,
                       USBD_CDC_IN_OUT_MAX_SIZE),
};

static char usbd_serial[PICO_UNIQUE_BOARD_ID_SIZE_BYTES * 2 + 1];

static const char *const usbd_strings[] = {
    [USBD_STR_MANUFACTURER] = "STPG",
    [USBD_STR_PRODUCT] = "STPG Pattern Generator v0.1",
    [USBD_STR_SERIAL] = usbd_serial,
    [USBD_STR_CDC] = "STPG CDC",
};

const uint8_t *tud_descriptor_device_cb(void) {
    return (const uint8_t *)&usbd_desc_device;
}

const uint8_t *tud_descriptor_configuration_cb(uint8_t index) {
    (void)index;
    return usbd_desc_cfg;
}

const uint16_t *tud_descriptor_string_cb(uint8_t index, uint16_t langid) {
    (void)langid;
    enum { DESC_STR_MAX = 64 };
    static uint16_t desc_str[DESC_STR_MAX];

    if (index == USBD_STR_LANG) {
        desc_str[1] = 0x0409;
        desc_str[0] = (uint16_t)((TUSB_DESC_STRING << 8) | 4);
        return desc_str;
    }

    if (usbd_serial[0] == '\0') {
        pico_get_unique_board_id_string(usbd_serial, sizeof(usbd_serial));
    }

    if (index >= (sizeof(usbd_strings) / sizeof(usbd_strings[0])) || usbd_strings[index] == 0) {
        return 0;
    }

    const char *str = usbd_strings[index];
    uint8_t len = 0;
    while (len < DESC_STR_MAX - 1 && str[len] != '\0') {
        desc_str[1 + len] = (uint8_t)str[len];
        ++len;
    }

    desc_str[0] = (uint16_t)((TUSB_DESC_STRING << 8) | (2 * len + 2));
    return desc_str;
}
