#ifndef WS2812_H
#define WS2812_H

/*
 * Minimal WS2812 (NeoPixel) driver for the 6-channel ADC board.
 *
 * Six WS2812 LEDs on a single data line (PB1), one per ADC port:
 *   address 0 = A0 (lowest), address 5 = A7.
 * Each LED shows green (active channel) or red (masked-out channel) at
 * WS2812_BRIGHT. The update is blocking and timing-safe: interrupts are masked
 * for the ~180 us bit-bang, and it is only called during config handling,
 * before the streaming loop (so the host is waiting for the ACK and sends
 * nothing mid-transfer).
 *
 * Timing is derived from the RS-485 test board's GD_WS2812_DRIVER.h, tuned for
 * a 48 MHz HCLK. Re-tune the NOP counts if the LEDs show the wrong colour.
 */

#include <stdint.h>

#include "debug.h"

#define WS2812_NUM_LEDS        6
#define WS2812_PORT            GPIOB
#define WS2812_PIN             GPIO_Pin_1 /* PB1 */
#define WS2812_BRIGHT_PERCENT  7
#define WS2812_BRIGHT          ((uint8_t)(255 * WS2812_BRIGHT_PERCENT / 100))
#define WS2812_RESET_US        80 /* latch: >50 us low */

static void ws2812_init(void)
{
    GPIO_InitTypeDef g = {0};
    RCC_PB2PeriphClockCmd(RCC_PB2Periph_GPIOB, ENABLE);
    g.GPIO_Pin = WS2812_PIN;
    g.GPIO_Mode = GPIO_Mode_Out_PP;
    g.GPIO_Speed = GPIO_Speed_30MHz;
    GPIO_Init(WS2812_PORT, &g);
    GPIO_WriteBit(WS2812_PORT, WS2812_PIN, Bit_RESET);
}

static void ws2812_send_bit(uint8_t bit)
{
    if (bit) {
        /* 1 bit: ~800 ns high, ~450 ns low */
        WS2812_PORT->BSHR = WS2812_PIN;
        __asm__("nop"); __asm__("nop"); __asm__("nop"); __asm__("nop");
        __asm__("nop"); __asm__("nop"); __asm__("nop"); __asm__("nop");
        __asm__("nop"); __asm__("nop"); __asm__("nop"); __asm__("nop");
        __asm__("nop"); __asm__("nop"); __asm__("nop"); __asm__("nop");
        WS2812_PORT->BCR = WS2812_PIN;
    } else {
        /* 0 bit: ~400 ns high, ~850 ns low */
        WS2812_PORT->BSHR = WS2812_PIN;
        __asm__("nop"); __asm__("nop"); __asm__("nop"); __asm__("nop");
        WS2812_PORT->BCR = WS2812_PIN;
        __asm__("nop"); __asm__("nop"); __asm__("nop"); __asm__("nop");
    }
}

static void ws2812_send_colour(uint8_t red, uint8_t green, uint8_t blue)
{
    /* WS2812 expects GRB order, MSB-first. */
    for (int8_t i = 7; i >= 0; i--) {
        ws2812_send_bit((green >> i) & 1);
    }
    for (int8_t i = 7; i >= 0; i--) {
        ws2812_send_bit((red >> i) & 1);
    }
    for (int8_t i = 7; i >= 0; i--) {
        ws2812_send_bit((blue >> i) & 1);
    }
}

/* Send one colour to every LED and latch. */
static void ws2812_set_all(uint8_t red, uint8_t green, uint8_t blue)
{
    __disable_irq();
    for (uint8_t i = 0; i < WS2812_NUM_LEDS; i++) {
        ws2812_send_colour(red, green, blue);
    }
    __enable_irq();
    Delay_Us(WS2812_RESET_US);
}

/* Set each LED green (active) or red (inactive) from a 6-bit channel mask. */
static void ws2812_set_channels(uint8_t mask)
{
    __disable_irq();
    for (uint8_t i = 0; i < WS2812_NUM_LEDS; i++) {
        if (mask & (1u << i)) {
            ws2812_send_colour(0, WS2812_BRIGHT, 0);   /* green = active */
        } else {
            ws2812_send_colour(WS2812_BRIGHT, 0, 0);   /* red = inactive */
        }
    }
    __enable_irq();
    Delay_Us(WS2812_RESET_US);
}

#endif /* WS2812_H */
