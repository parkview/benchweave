/********************************** (C) COPYRIGHT *******************************
 * File Name          : main.c
 * Author             : Stephen Eaton
 * Version            : V0.2.0
 * Date               : 2026-09-12
 * Description        : BenchWeave ADC board firmware.
 *                      6-channel 12-bit ADC streaming over UART at 2 Mbps,
 *                      with master-driven command/control.
 *
 * Wire protocol (see BenchWeave spec
 *   docs/superpowers/specs/2026-09-11-adc-board-uart-driver-design.md):
 *
 *   Frame = [0xAA 0x55][type][seq][len][payload(0..len-1)][crc16 LE]
 *   CRC16 = CCITT-FALSE (poly 0x1021, init 0xFFFF) over type..payload.
 *
 * ADC GPIO (canonical channel order):
 *   [0] PA2 = A0   [1] PA6 = A1   [2] PC4 = A2
 *   [3] PD2 = A3   [4] PD3 = A4   [5] PD4 = A7
 *
 * Other GPIO:
 *   PC1 = green LED   PA3 = trigger in (reserved)   PC2 = trigger out (reserved)
 *
 * Hardware: USART1 TX=PD5, RX=PD6, connected to the CH343G USB-UART bridge.
 *********************************************************************************
 * Copyright (c) Open Source
 *******************************************************************************/

#include "debug.h"
#include <stdint.h>

/* ── Frame types ─────────────────────────────────────────────────────────── */
#define TYPE_SET_AVERAGING   0x01
#define TYPE_SET_CHANNELS    0x02
#define TYPE_SET_SAMPLE_MODE 0x03
#define TYPE_START_STREAM    0x04
#define TYPE_STOP_STREAM     0x05
#define TYPE_SAMPLE_ONCE     0x06
#define TYPE_RESET           0x07
#define TYPE_IDENTIFY        0x08
#define TYPE_ARM_TRIGGER     0x09 /* reserved: external trigger not implemented */
#define TYPE_ACK             0x81
#define TYPE_NAK             0x82
#define TYPE_IDENTIFY_RSP    0x83
#define TYPE_SAMPLE          0x90

/* ── Error codes ─────────────────────────────────────────────────────────── */
#define ERR_BAD_COMMAND     0x01
#define ERR_BAD_PARAMETER   0x02
#define ERR_BUSY            0x03
#define ERR_UNSUPPORTED     0x04

/* ── Protocol constants ──────────────────────────────────────────────────── */
#define SYNC0               0xAA
#define SYNC1               0x55
#define N_CHANNELS          6
#define MAX_RX_PAYLOAD      16
#define PROTOCOL_VERSION    1
#define FW_MAJOR            0
#define FW_MINOR            2

/* ── GPIO ────────────────────────────────────────────────────────────────── */
#define LED_PIN             GPIO_Pin_1 /* PC1 green LED */

/* ToDo (deferred): drive the 6x WS2812 status LEDs on PB1, one per port.
 *   Address 0 = A0 .. 5 = A7. Green = active (15% brightness), red = inactive.
 *   Blocking refresh during hardware-config only, before the streaming loop.
 *   Reuse GD_WS2812_DRIVER.h (adapt num_leds=6, [6][3] RGB buffer, PB1).
 *   See spec "Deferred firmware features". */

/* ── Sampling state ──────────────────────────────────────────────────────── */
#define STREAM_IDLE         0
#define STREAM_STREAMING    1

/* ADC channel numbers in canonical order. */
static const uint8_t ADC_CHANNELS[N_CHANNELS] = {0, 1, 2, 3, 4, 7};

static uint8_t stream_state = STREAM_IDLE;
static uint8_t sample_once_pending = 0;
static uint16_t avg_count = 0;      /* 0 (raw), 4, 8, 16, 32, 64, 128, 256 */
static uint8_t avg_shift = 0;       /* log2(avg_count); 0 when raw */
static uint8_t channel_mask = 0x3F; /* all 6 channels */
static uint32_t sample_counter = 0; /* SAMPLE frame sequence (gap detection) */

/* ── TX sequence ─────────────────────────────────────────────────────────── */
static uint8_t tx_seq = 0;

/* ── RX ring buffer (USART ISR -> main loop) ────────────────────────────── */
#define RX_BUF_SIZE 64
static volatile uint8_t rx_buf[RX_BUF_SIZE];
static volatile uint8_t rx_head = 0;
static volatile uint8_t rx_tail = 0;

static uint8_t rx_available(void) {
    return rx_head != rx_tail;
}

static uint8_t rx_pop(void) {
    uint8_t b = rx_buf[rx_tail];
    rx_tail = (uint8_t)((rx_tail + 1) % RX_BUF_SIZE);
    return b;
}

/* Called from the USART1 ISR. Single producer (ISR), single consumer (main). */
void adc_rx_push(uint8_t b) {
    uint8_t next = (uint8_t)((rx_head + 1) % RX_BUF_SIZE);
    if (next != rx_tail) {
        rx_buf[rx_head] = b;
        rx_head = next;
    }
    /* else: ring full -> drop (commands are small and rare). */
}

/* ── CRC16 CCITT-FALSE (running) ─────────────────────────────────────────── */
static uint16_t crc16_update(uint16_t crc, uint8_t b) {
    crc ^= (uint16_t)b << 8;
    for (uint8_t i = 0; i < 8; i++) {
        if (crc & 0x8000)
            crc = (uint16_t)((crc << 1) ^ 0x1021);
        else
            crc = (uint16_t)(crc << 1);
    }
    return crc;
}

/* ── UART TX (blocking byte) ─────────────────────────────────────────────── */
static void uart_tx_byte(uint8_t b) {
    while (!(USART1->STATR & USART_STATR_TXE))
        ;
    USART1->DATAR = b;
}

/* ── Frame encoder + TX ──────────────────────────────────────────────────── */
static void send_frame(uint8_t type, const uint8_t *payload, uint8_t len) {
    uint16_t crc = 0xFFFF;
    crc = crc16_update(crc, type);
    crc = crc16_update(crc, tx_seq);
    crc = crc16_update(crc, len);
    for (uint8_t i = 0; i < len; i++) {
        crc = crc16_update(crc, payload[i]);
    }

    uart_tx_byte(SYNC0);
    uart_tx_byte(SYNC1);
    uart_tx_byte(type);
    uart_tx_byte(tx_seq);
    tx_seq++;
    uart_tx_byte(len);
    for (uint8_t i = 0; i < len; i++) {
        uart_tx_byte(payload[i]);
    }
    uart_tx_byte((uint8_t)(crc & 0xFF));
    uart_tx_byte((uint8_t)(crc >> 8));
}

/* ── Response builders ───────────────────────────────────────────────────── */
static void send_ack(uint8_t echo_type, uint16_t value) {
    uint8_t p[3];
    p[0] = echo_type;
    p[1] = (uint8_t)(value & 0xFF);
    p[2] = (uint8_t)(value >> 8);
    send_frame(TYPE_ACK, p, 3);
}

static void send_nak(uint8_t echo_type, uint8_t error) {
    uint8_t p[2];
    p[0] = echo_type;
    p[1] = error;
    send_frame(TYPE_NAK, p, 2);
}

static void send_identify(void) {
    uint8_t p[5];
    p[0] = PROTOCOL_VERSION;
    p[1] = FW_MAJOR;
    p[2] = FW_MINOR;
    p[3] = N_CHANNELS;
    p[4] = 12; /* resolution bits */
    send_frame(TYPE_IDENTIFY_RSP, p, 5);
}

static void send_sample(const uint16_t ch[N_CHANNELS]) {
    uint8_t p[4 + 2 * N_CHANNELS];
    p[0] = (uint8_t)(sample_counter & 0xFF);
    p[1] = (uint8_t)((sample_counter >> 8) & 0xFF);
    p[2] = (uint8_t)((sample_counter >> 16) & 0xFF);
    p[3] = (uint8_t)((sample_counter >> 24) & 0xFF);
    for (uint8_t i = 0; i < N_CHANNELS; i++) {
        p[4 + 2 * i] = (uint8_t)(ch[i] & 0xFF);
        p[5 + 2 * i] = (uint8_t)(ch[i] >> 8);
    }
    send_frame(TYPE_SAMPLE, p, 4 + 2 * N_CHANNELS);
    sample_counter++;
}

/* ── RX state machine ────────────────────────────────────────────────────── */
typedef enum {
    RX_SYNC0,
    RX_SYNC1,
    RX_TYPE,
    RX_SEQ,
    RX_LEN,
    RX_PAYLOAD,
    RX_CRC_LO,
    RX_CRC_HI
} rx_state_t;

static rx_state_t rx_state = RX_SYNC0;
static uint8_t frame_type = 0;
static uint8_t frame_len = 0;
static uint8_t frame_payload[MAX_RX_PAYLOAD];
static uint8_t frame_payload_idx = 0;
static uint16_t rx_crc = 0;
static uint8_t frame_crc_lo = 0;

static uint8_t valid_averaging(uint16_t n) {
    switch (n) {
    case 0:
    case 4:
    case 8:
    case 16:
    case 32:
    case 64:
    case 128:
    case 256:
        return 1;
    default:
        return 0;
    }
}

static uint8_t log2_shift(uint16_t n) {
    uint8_t s = 0;
    while (n > 1) {
        n >>= 1;
        s++;
    }
    return s;
}

static void handle_frame(uint8_t type, const uint8_t *payload, uint8_t len);

static void rx_byte(uint8_t b) {
    switch (rx_state) {
    case RX_SYNC0:
        if (b == SYNC0)
            rx_state = RX_SYNC1;
        break;

    case RX_SYNC1:
        if (b == SYNC1)
            rx_state = RX_TYPE;
        else if (b != SYNC0)
            rx_state = RX_SYNC0;
        break;

    case RX_TYPE:
        frame_type = b;
        rx_crc = crc16_update(0xFFFF, b);
        rx_state = RX_SEQ;
        break;

    case RX_SEQ:
        /* seq is not echoed, but it is part of the CRC. */
        rx_crc = crc16_update(rx_crc, b);
        rx_state = RX_LEN;
        break;

    case RX_LEN:
        frame_len = b;
        rx_crc = crc16_update(rx_crc, b);
        if (frame_len > MAX_RX_PAYLOAD) {
            rx_state = RX_SYNC0; /* corrupt length -> resync */
        } else if (frame_len == 0) {
            rx_state = RX_CRC_LO;
        } else {
            frame_payload_idx = 0;
            rx_state = RX_PAYLOAD;
        }
        break;

    case RX_PAYLOAD:
        frame_payload[frame_payload_idx++] = b;
        rx_crc = crc16_update(rx_crc, b);
        if (frame_payload_idx == frame_len)
            rx_state = RX_CRC_LO;
        break;

    case RX_CRC_LO:
        frame_crc_lo = b;
        rx_state = RX_CRC_HI;
        break;

    case RX_CRC_HI: {
        uint16_t received = (uint16_t)(((uint16_t)b << 8) | frame_crc_lo);
        rx_state = RX_SYNC0;
        if (received == rx_crc) {
            handle_frame(frame_type, frame_payload, frame_len);
        }
        /* else: bad CRC -> silently drop (resync). */
        break;
    }
    }
}

static void handle_frame(uint8_t type, const uint8_t *payload, uint8_t len) {
    switch (type) {
    case TYPE_IDENTIFY:
        send_identify();
        break;

    case TYPE_SET_AVERAGING:
        if (len >= 2) {
            uint16_t n = (uint16_t)(payload[0] | ((uint16_t)payload[1] << 8));
            if (valid_averaging(n)) {
                avg_count = n;
                avg_shift = log2_shift(n);
                send_ack(type, n);
            } else {
                send_nak(type, ERR_BAD_PARAMETER);
            }
        } else {
            send_nak(type, ERR_BAD_PARAMETER);
        }
        break;

    case TYPE_SET_CHANNELS:
        if (len >= 1 && payload[0] <= 0x3F) {
            channel_mask = payload[0];
            send_ack(type, (uint16_t)payload[0]);
        } else {
            send_nak(type, ERR_BAD_PARAMETER);
        }
        break;

    case TYPE_SET_SAMPLE_MODE:
        if (len >= 1 && payload[0] == 0) {
            send_ack(type, 0); /* free-run */
        } else if (len >= 1) {
            send_nak(type, ERR_UNSUPPORTED); /* cycle/timer reserved */
        } else {
            send_nak(type, ERR_BAD_PARAMETER);
        }
        break;

    case TYPE_START_STREAM:
        stream_state = STREAM_STREAMING;
        send_ack(type, 0);
        break;

    case TYPE_STOP_STREAM:
        stream_state = STREAM_IDLE;
        send_ack(type, 0);
        break;

    case TYPE_SAMPLE_ONCE:
        sample_once_pending = 1;
        send_ack(type, 0);
        break;

    case TYPE_RESET:
        send_ack(type, 0);
        Delay_Ms(10);
        NVIC_SystemReset();
        break;

    default:
        send_nak(type, ERR_BAD_COMMAND);
        break;
    }
}

/* ── ADC ─────────────────────────────────────────────────────────────────── */
static void gpio_analog_init(void) {
    GPIO_InitTypeDef g = {0};
    RCC_PB2PeriphClockCmd(RCC_PB2Periph_GPIOA | RCC_PB2Periph_GPIOC | RCC_PB2Periph_GPIOD, ENABLE);

    g.GPIO_Mode = GPIO_Mode_AIN;
    g.GPIO_Pin = GPIO_Pin_2 | GPIO_Pin_6; /* PA2=A0, PA6=A1 */
    GPIO_Init(GPIOA, &g);
    g.GPIO_Pin = GPIO_Pin_4; /* PC4=A2 */
    GPIO_Init(GPIOC, &g);
    g.GPIO_Pin = GPIO_Pin_2 | GPIO_Pin_3 | GPIO_Pin_4; /* PD2=A3, PD3=A4, PD4=A7 */
    GPIO_Init(GPIOD, &g);
}

static void adc_init_12bit(void) {
    ADC_InitTypeDef a = {0};
    RCC_PB2PeriphClockCmd(RCC_PB2Periph_ADC1, ENABLE);
    RCC_ADCCLKConfig(RCC_PCLK2_Div8);

    ADC_DeInit(ADC1);
    a.ADC_Mode = ADC_Mode_Independent;
    a.ADC_ScanConvMode = DISABLE;
    a.ADC_ContinuousConvMode = DISABLE;
    a.ADC_ExternalTrigConv = ADC_ExternalTrigConv_None;
    a.ADC_DataAlign = ADC_DataAlign_Right; /* 12-bit, right-aligned (0..4095) */
    a.ADC_NbrOfChannel = 1;
    ADC_Init(ADC1, &a);
    ADC_Cmd(ADC1, ENABLE);
}

static uint16_t adc_read(uint8_t channel) {
    ADC_RegularChannelConfig(ADC1, channel, 1, ADC_SampleTime_CyclesMode5);
    ADC_SoftwareStartConvCmd(ADC1, ENABLE);
    while (!ADC_GetFlagStatus(ADC1, ADC_FLAG_EOC))
        ;
    return (uint16_t)ADC_GetConversionValue(ADC1);
}

static void sample_all(uint16_t out[N_CHANNELS]) {
    uint32_t samples = (avg_count == 0) ? 1u : (uint32_t)avg_count;
    for (uint8_t c = 0; c < N_CHANNELS; c++) {
        if (!(channel_mask & (1u << c))) {
            out[c] = 0; /* channel disabled */
            continue;
        }
        uint32_t sum = 0;
        for (uint32_t i = 0; i < samples; i++) {
            sum += adc_read(ADC_CHANNELS[c]);
        }
        out[c] = (uint16_t)(sum >> avg_shift);
    }
}

/* ── USART (2 Mbps, interrupt RX) ────────────────────────────────────────── */
static void usart_init_2m(void) {
    GPIO_InitTypeDef g = {0};
    USART_InitTypeDef u = {0};
    RCC_PB2PeriphClockCmd(RCC_PB2Periph_GPIOD | RCC_PB2Periph_USART1, ENABLE);

    g.GPIO_Pin = GPIO_Pin_5; /* TX PD5 */
    g.GPIO_Speed = GPIO_Speed_30MHz;
    g.GPIO_Mode = GPIO_Mode_AF_PP;
    GPIO_Init(GPIOD, &g);

    g.GPIO_Pin = GPIO_Pin_6; /* RX PD6 */
    g.GPIO_Mode = GPIO_Mode_IN_FLOATING;
    GPIO_Init(GPIOD, &g);

    u.USART_BaudRate = 2000000;
    u.USART_WordLength = USART_WordLength_8b;
    u.USART_StopBits = USART_StopBits_1;
    u.USART_Parity = USART_Parity_No;
    u.USART_HardwareFlowControl = USART_HardwareFlowControl_None;
    u.USART_Mode = USART_Mode_Tx | USART_Mode_Rx;
    USART_Init(USART1, &u);

    USART_ITConfig(USART1, USART_IT_RXNE, ENABLE);
    NVIC_EnableIRQ(USART1_IRQn);
    USART_Cmd(USART1, ENABLE);
}

/* ── Green LED (PC1): solid on while streaming, slow blink when idle ────── */
static uint8_t led_state = 0;

static void led_init(void) {
    GPIO_InitTypeDef g = {0};
    RCC_PB2PeriphClockCmd(RCC_PB2Periph_GPIOC, ENABLE);
    g.GPIO_Pin = LED_PIN;
    g.GPIO_Mode = GPIO_Mode_Out_PP;
    g.GPIO_Speed = GPIO_Speed_30MHz;
    GPIO_Init(GPIOC, &g);
    GPIO_WriteBit(GPIOC, LED_PIN, Bit_RESET);
}

static void led_on(void) {
    led_state = 1;
    GPIO_WriteBit(GPIOC, LED_PIN, Bit_SET);
}

static void led_off(void) {
    led_state = 0;
    GPIO_WriteBit(GPIOC, LED_PIN, Bit_RESET);
}

static void led_toggle(void) {
    led_state = (uint8_t)(led_state ^ 1);
    GPIO_WriteBit(GPIOC, LED_PIN, led_state ? Bit_SET : Bit_RESET);
}

static void led_boot_blink(void) {
    for (uint8_t i = 0; i < 3; i++) {
        led_on();
        Delay_Ms(80);
        led_off();
        Delay_Ms(80);
    }
}

/* ── Main ────────────────────────────────────────────────────────────────── */
/* Approximate idle heartbeat period (loop-iteration counter). */
#define HEARTBEAT_TICKS 100000

int main(void) {
    uint16_t samples[N_CHANNELS];
    uint32_t idle_ticks = 0;

    NVIC_PriorityGroupConfig(NVIC_PriorityGroup_1);
    SystemCoreClockUpdate();
    Delay_Init();
    Delay_Ms(100);
    GPIO_PinRemapConfig(GPIO_Remap_PA1_2, DISABLE); /* PA1/PA2 as GPIO (A0 = PA2) */

    led_init();
    gpio_analog_init();
    adc_init_12bit();
    usart_init_2m();

    led_boot_blink();

    while (1) {
        /* 1. Drain RX ring buffer through the frame state machine. */
        while (rx_available()) {
            rx_byte(rx_pop());
        }

        /* 2. Stream / single-shot sampling. */
        if (stream_state == STREAM_STREAMING) {
            led_on();
            sample_all(samples);
            send_sample(samples);
        } else if (sample_once_pending) {
            sample_once_pending = 0;
            sample_all(samples);
            send_sample(samples);
        } else {
            if (++idle_ticks >= HEARTBEAT_TICKS) {
                idle_ticks = 0;
                led_toggle();
            }
        }
    }
}
