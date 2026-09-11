/********************************** (C) COPYRIGHT *******************************
 * File Name          : main.c
 * Author             : Parkview
 * Version            : V0.0.1
 * Date               : 2026/09/10
 * Description        : Test ADC code for the 6 channel ADC v0.1 PCB
 *********************************************************************************
 * Copyright (c) Open Source
 * 
 

Possible ADC GPIO:
------------------
PA2 A0
PA6 A1
PC4 A2
PD2 A3
PD3 A4
PD4 A7
A8 Vref (internal)
A9 ?? Vcal??

Other GPIO:
-----------
PB1 RGB LEDs
PA3 Trigger in
PA5 SW2
PA4 SW1
PC1 LED
PC2 Trigger out

*******************************************************************************/

/*
 *@Note

 *
 *Hardware connection:PD5 -- Rx
 *                    PD6 -- Tx
 *  Connects to the CH343G USB-UART pins
 */

#include "debug.h"
#include <math.h>

/* Global define */


/* Global Variable */
vu8 val;
const uint16_t R271_ADC_PIN = GPIO_Pin_2;  // PD2, R27 pin 1 ADC A3 input
const uint16_t R272_ADC_PIN = GPIO_Pin_3;  // PD3, R27 pin 2 ADC A4 input
const uint16_t ADC7_PIN = GPIO_Pin_4;      // PD4,  ADC A7 input
const uint16_t ADC2_PIN = GPIO_Pin_4;      // PC4,  ADC A2 input
const uint16_t R321_ADC_PIN = GPIO_Pin_6;  // PA6, R32 pin 1 ADC A1 input
const uint16_t EN_ADC_PIN = GPIO_Pin_2;    // PA2, EN Pin DC-DC Regulator ADC A0 input

const uint16_t LED1_PIN = GPIO_Pin_1;      // PC1, LED1 Green output
const uint16_t SW1_PIN = GPIO_Pin_4;       // PA4, SW1 input
const uint16_t SW2_PIN = GPIO_Pin_5;       // PA5, SW2 input
const uint16_t RGB_PIN = GPIO_Pin_1;       // PB1, RGB output 
const uint16_t TRIG_IN_PIN = GPIO_Pin_3;   // PA3, trigger input
const uint16_t TRIG_OUT_PIN = GPIO_Pin_2;  // PC2, trigger output

s16 Calibration_Val = 0;

// If you want to compute Vdd from channel8 (bandgap):
static const float VBG_EST = 1.196f;  // My estimate (1.29f using 1 cycle conversion time) (1.23F using a 7 cycle conversion time) of the Vref on the Soil Sensor Dev board v0.1b using a 1 cycle conversion time
// static const float VBG_EST = 1.199071062271062f;  // estimated bandgap
// static const float ADC_MAX = 4095.0f;
static const float ADC_MAX = 1024.0f;

u16 ReadPhotoTransistorVoltage (void) {
    // return one light sample from the PhotoTransistor NOTE: Note fitted by default on the v0.1d PCB!!!!
    // go read the 1st ADC channel (PA1) to find out the light current (voltage) reading reading
    u16 val;

    ADC_RegularChannelConfig (ADC1, 1, 1, ADC_SampleTime_CyclesMode5);
    ADC_SoftwareStartConvCmd (ADC1, ENABLE);
    while (!ADC_GetFlagStatus (ADC1, ADC_FLAG_EOC));
    val = ADC_GetConversionValue (ADC1);
    printf ("Vlight_raw: %d\r\n", val);
    return val;
}

void ADC_Function_Init (void) {

    ADC_InitTypeDef ADC_InitStructure = {0};
    GPIO_InitTypeDef GPIO_InitStructure = {0};
    RCC_PB2PeriphClockCmd (RCC_PB2Periph_GPIOA | RCC_PB2Periph_GPIOC | RCC_PB2Periph_GPIOD, ENABLE);
    RCC_PB2PeriphClockCmd (RCC_PB2Periph_ADC1, ENABLE);
    RCC_ADCCLKConfig (RCC_PCLK2_Div8);

    GPIO_InitStructure.GPIO_Pin = EN_ADC_PIN | R321_ADC_PIN;  // set ADC inputs for port A
    GPIO_InitStructure.GPIO_Mode = GPIO_Mode_AIN;
    GPIO_Init (GPIOA, &GPIO_InitStructure);

    GPIO_InitStructure.GPIO_Pin = ADC2_PIN;        // set ADC inputs for Port C pins
    GPIO_InitStructure.GPIO_Mode = GPIO_Mode_AIN;
    GPIO_Init (GPIOC, &GPIO_InitStructure);

    GPIO_InitStructure.GPIO_Pin = R271_ADC_PIN | R272_ADC_PIN | ADC7_PIN;  // set ADC inputs for Port D pins
    GPIO_InitStructure.GPIO_Mode = GPIO_Mode_AIN;
    GPIO_Init (GPIOD, &GPIO_InitStructure);

    ADC_DeInit (ADC1);
    ADC_InitStructure.ADC_Mode = ADC_Mode_Independent;
    ADC_InitStructure.ADC_ScanConvMode = ENABLE;
    ADC_InitStructure.ADC_ContinuousConvMode = DISABLE;
    ADC_InitStructure.ADC_ExternalTrigConv = ADC_ExternalTrigConv_None;
    ADC_InitStructure.ADC_DataAlign = ADC_DataAlign_Right;
    ADC_InitStructure.ADC_NbrOfChannel = 1;
    ADC_Init (ADC1, &ADC_InitStructure);

    ADC_ExternalTrigInjectedConvConfig (ADC1, ADC_ExternalTrigInjecConv_T1_CC3);

    ADC_InjectedSequencerLengthConfig (ADC1, 4);
    ADC_InjectedChannelConfig (ADC1, ADC_Channel_0, 0, ADC_SampleTime_CyclesMode5);
    ADC_InjectedChannelConfig (ADC1, ADC_Channel_1, 1, ADC_SampleTime_CyclesMode5);
    ADC_InjectedChannelConfig (ADC1, ADC_Channel_2, 2, ADC_SampleTime_CyclesMode5);
    ADC_InjectedChannelConfig (ADC1, ADC_Channel_3, 3, ADC_SampleTime_CyclesMode5);
    // ADC_InjectedChannelConfig (ADC1, ADC_Channel_4, 4, ADC_SampleTime_15Cycles);
    ADC_InjectedChannelConfig (ADC1, ADC_Channel_7, 7, ADC_SampleTime_CyclesMode5);

    ADC_DiscModeChannelCountConfig (ADC1, 1);
    ADC_InjectedDiscModeCmd (ADC1, ENABLE);
    ADC_ExternalTrigInjectedConvCmd (ADC1, ENABLE);
    ADC_Cmd (ADC1, ENABLE);
}

/*********************************************************************
 * @fn      Pulse LED
 *
 * @brief   flashes User LED PC6
 *
 * @return  none
 */
void Flash_PC6_LED (void) {
    GPIO_WriteBit (GPIOC, LED1_PIN, Bit_SET);    // turn on User LED
    Delay_Ms (250);
    GPIO_WriteBit (GPIOC, LED1_PIN, Bit_RESET);  // turn on User LED
    Delay_Ms (250);
}

/*********************************************************************
 * @fn      GPIO-PC_Toggle_INIT
 *
 * @brief   Initializes GPIOC for Outputs
 *
 * @return  none
 */
void GPIOC_Toggle_INIT (void) {
    GPIO_InitTypeDef GPIO_InitStructure = {0};
    RCC_PB2PeriphClockCmd (RCC_PB2Periph_GPIOC, ENABLE);
    GPIO_InitStructure.GPIO_Pin = LED1_PIN;
    GPIO_InitStructure.GPIO_Mode = GPIO_Mode_Out_PP;
    GPIO_InitStructure.GPIO_Speed = GPIO_Speed_30MHz;  //
    GPIO_Init (GPIOC, &GPIO_InitStructure);
}

/*********************************************************************
 * @fn      USARTx_CFG
 *
 * @brief   Initializes the USART1 peripheral.
 *
 * @return  none
 */
void USARTx_CFG (void) {
    GPIO_InitTypeDef GPIO_InitStructure = {0};
    USART_InitTypeDef USART_InitStructure = {0};

    RCC_PB2PeriphClockCmd (RCC_PB2Periph_GPIOD | RCC_PB2Periph_USART1, ENABLE);

    /* USART1 TX-->D.5   RX-->D.6 */
    GPIO_InitStructure.GPIO_Pin = GPIO_Pin_5;
    GPIO_InitStructure.GPIO_Speed = GPIO_Speed_30MHz;
    GPIO_InitStructure.GPIO_Mode = GPIO_Mode_AF_PP;
    GPIO_Init (GPIOD, &GPIO_InitStructure);
    GPIO_InitStructure.GPIO_Pin = GPIO_Pin_6;
    GPIO_InitStructure.GPIO_Mode = GPIO_Mode_IN_FLOATING;
    GPIO_Init (GPIOD, &GPIO_InitStructure);

    USART_InitStructure.USART_BaudRate = 115200;
    USART_InitStructure.USART_WordLength = USART_WordLength_8b;
    USART_InitStructure.USART_StopBits = USART_StopBits_1;
    USART_InitStructure.USART_Parity = USART_Parity_No;
    USART_InitStructure.USART_HardwareFlowControl = USART_HardwareFlowControl_None;
    USART_InitStructure.USART_Mode = USART_Mode_Tx | USART_Mode_Rx;

    USART_Init (USART1, &USART_InitStructure);
    USART_Cmd (USART1, ENABLE);
}

u16 Get_ADC_Val (u8 ch) {
    // This does a single ADC data conversion and
    u32 val;

    ADC_RegularChannelConfig (ADC1, ch, 1, ADC_SampleTime_CyclesMode5);
    ADC_SoftwareStartConvCmd (ADC1, ENABLE);
    while (!ADC_GetFlagStatus (ADC1, ADC_FLAG_EOC));
    // GPIO_WriteBit (GPIOC, GPIO_Pin_6, Bit_SET);
    // GPIO_WriteBit (GPIOC, GPIO_Pin_6, Bit_RESET);
    val = ADC_GetConversionValue (ADC1);
    // GPIO_WriteBit (GPIOC, GPIO_Pin_6, Bit_RESET);
    //  printf ("ADC-raw: %d\r\n", val);
    return val;
}

u16 get_Vref (void) {
    // go read the 8th ADC channel to find out the current Vref voltage reading
    u16 val;

    ADC_RegularChannelConfig (ADC1, 8, 1, ADC_SampleTime_CyclesMode5);
    ADC_SoftwareStartConvCmd (ADC1, ENABLE);
    while (!ADC_GetFlagStatus (ADC1, ADC_FLAG_EOC));
    val = ADC_GetConversionValue (ADC1);
    // printf ("Vref findings:");
    // printf ("Vref_raw: %d\r\n", val);
    // printf("ADC/4096 = %");
    return val;
}

u16 Get_ConversionVal (s16 val) {
    if ((val + Calibration_Val) < 0 || val == 0)
        return 0;
    if ((Calibration_Val + val) > 4095 || val == 4095)
        return 4095;
    return (val + Calibration_Val);
}

float calculateVoltage (s16 ADCbits) {
    float vref = 3.25f;
    float adcVoltage = (vref * ADCbits) / 1024.0f;
    return adcVoltage;
}

void printADCVoltage (u16 adcValue) {
    if (adcValue == 0) {
        printf ("ADC Value is zero\n");
    } else {
        float adcVoltage = calculateVoltage (adcValue);
        int wholePart = (int)adcVoltage;
        int decimalPart = (int)((adcVoltage - wholePart) * 10000);
        printf ("%d.%04dV\n\r", wholePart, decimalPart);
    }
}

s16 getVbatt (void) {

    s16 temp = Get_ADC_Val (ADC_Channel_3);  // Vbatt is on ADC channel 3, Port C. Should be around 1/3 of Battery voltage
    // printf ("\r\nADC3: %d\r\n", temp);
    s16 ntcVoltage = Get_ConversionVal (temp);
    // printf ("\r\nVbatt: %dV\r\n", ntcVoltage);

    // printf ("SuperCap Voltage: %.2f C\r\n", celsius);
    return ntcVoltage;
}

float Vdd_from_R8 (uint16_t R8) {
    // if you want an alternate computation via channel8
    if (R8 == 0)
        return 0.0f;
    return (VBG_EST * ADC_MAX) / (float)R8;
}

float ln_approx (float x) {
    if (x <= 0.0f)
        return 0.0f;  // avoid log(0)

    int k = 0;
    // Scale x to range 0.5..2 using ln(a*b) = ln(a) + ln(b)
    while (x > 2.0f) {
        x *= 0.5f;
        k++;
    }
    while (x < 0.5f) {
        x *= 2.0f;
        k--;
    }
    // Use 4-term series ln(1+y) ?им??W y - y^2/2 + y^3/3 - y^4/4, y = x-1
    float y = x - 1.0f;
    float y2 = y * y;
    float y3 = y2 * y;
    float y4 = y3 * y;
    float ln1 = y - y2 * 0.5f + y3 / 3.0f - y4 * 0.25f;
    // Adjust for scaling: ln(x * 2^k) = ln(x) + k*ln(2)
    const float LN2 = 0.69314718f;
    return ln1 + k * LN2;
}

/*********************************************************************
 * @fn      main
 *
 * @brief   Main program.
 *
 * @return  none
 */
int main (void) {

    u16 wholePart = 0;    // used in printf float conversions
    u16 decimalPart = 0;  // used in printf float conversions
                          // uint16_t adc_val = 0;

    NVIC_PriorityGroupConfig (NVIC_PriorityGroup_1);
    SystemCoreClockUpdate();
    Delay_Init();
    Delay_Ms (1000);
    GPIO_PinRemapConfig (GPIO_Remap_PA1_2, DISABLE);  // disable xtal use for PA1/PA2 and remap them to general I/O
#if (SDI_PRINT == SDI_PR_OPEN)
    SDI_Printf_Enable();
#else
    USART_Printf_Init (115200);
#endif
    printf ("SystemClk:%d\r\n", SystemCoreClock);
    printf ("ChipID:%08x\r\n\n", DBGMCU_GetCHIPID());

    USARTx_CFG();

    GPIOC_Toggle_INIT();
    ADC_Function_Init();     // setup config for PDn and PA2 ADC GPIO
   // Flash_PC6_LED();         // White LED

    //ADC_Cmd (ADC1, ENABLE);  // This draws 170-190uA  Turn it off when it's finished
    // uint16_t r8 = get_Vref();
    //   GPIO_WriteBit (GPIOC, GPIO_Pin_6, Bit_RESET);
    // float vdd = Vdd_from_R8 (r8);  // optional sanity-check

    // adc_val = Get_ADC_Val (4);  // your 10-bit ADC reading

    // float vdd = (VBG_EST * ADC_MAX) / (float)r8;
    // wholePart = (int)vdd;
    // decimalPart = (int)((vdd - wholePart) * 10000);
    // printf ("Vdd: %d.%d, R8: %d\r\n", wholePart, decimalPart, r8);
    uint16_t r8 = get_Vref();
    float vdd1 = Vdd_from_R8 (r8);  // optional sanity-check
    vdd1 = (VBG_EST * ADC_MAX) / (float)r8;
    wholePart = (int)vdd1;
    decimalPart = (int)((vdd1 - wholePart) * 10000);
    printf ("Vdd: %d.%d, raw: %d\r\n", wholePart, decimalPart, r8);
    // Test out PC4 - A2 ADC channel
    uint32_t adc_val4 = Get_ADC_Val (2);  // your 10-bit ADC Dummy reading
    adc_val4 = Get_ADC_Val (2);           // your 10-bit ADC Dummy reading
                                          //  uint16_t r8 = get_Vref();
    // float vdd2 = Vdd_from_R8 (adc_val2);  // optional sanity-check
    float vdd2 = 2 * ((float)adc_val4 * vdd1) / 1023.0f;
    // vdd1 = (VBG_EST * ADC_MAX) / (float)adc_val2;
    wholePart = (int)vdd2;
    decimalPart = (int)((vdd2 - wholePart) * 10000);
    printf ("EN Pin: %d.%d, raw: %d\r\n", wholePart, decimalPart, adc_val4);
    while (1) {
        // grab the DC-DC output voltage.  Note: it's half of what it should be, so times 2
        uint32_t adc_val7 = Get_ADC_Val (1);  // your 10-bit ADC Dummy reading
        adc_val7 = 0;
        for (u8 i = 0; i < 16; i++) {
            adc_val7 += Get_ADC_Val (1);  // your 10-bit ADC reading
        }
        adc_val7 >>= 4;                   // divide by 256 to average out 256 readings
        float vdd7 = 2 * ((float)adc_val7 * vdd1) / 1023.0f;
        uint16_t wholePart7 = (int)vdd7;
        uint16_t decimalPart7 = (int)((vdd7 - wholePart7) * 10000);
        // get PD2=R27.1 - Cap current
        uint32_t adc_val2 = Get_ADC_Val (3);  // your 10-bit ADC DUMMY reading
        adc_val2 = 0;
        for (u8 i = 0; i < 255; i++) {
            adc_val2 += Get_ADC_Val (3);  // your 10-bit ADC reading
        }
        adc_val2 >>= 8;                   // divide by 256 to average out 256 readings
        float vdd2 = ((float)adc_val2 * vdd1) / 1023.0f;
        uint16_t wholePart2 = (int)vdd2;
        uint16_t decimalPart2 = (int)((vdd2 - wholePart2) * 10000);
        // printf ("R271: %d.%d, raw: %d\r\n", wholePart2, decimalPart2, adc_val2);
        // get PD3=R27.2 - cap current
        uint32_t adc_val3 = Get_ADC_Val (4);  // your 10-bit ADC Dummy reading
        adc_val3 = 0;
        for (u8 i = 0; i < 255; i++) {
            adc_val3 += Get_ADC_Val (4);  // your 10-bit ADC reading
        }
        adc_val3 >>= 8;                   // divide by 256 to average out 256 readings
        float vdd3 = ((float)adc_val3 * vdd1) / 1023.0f;
        uint16_t wholePart3 = (int)vdd3;
        uint16_t decimalPart3 = (int)((vdd3 - wholePart3) * 10000);
        // go get the EN Pin voltage
        adc_val4 = Get_ADC_Val (0);  // your 10-bit ADC Dummy reading
        adc_val4 = 0;
        for (u8 i = 0; i < 255; i++) {
            adc_val4 += Get_ADC_Val (0);  // your 10-bit ADC reading
        }
        adc_val4 >>= 8;                   // divide by 256 to average out 256 readings
        float vdd4 = (((float)adc_val4 * vdd1) / 1023.0f) * 2.0f;
        uint16_t wholePart4 = (int)vdd4;
        uint16_t decimalPart4 = (int)((vdd4 - wholePart4) * 10000);
        // display:  ADC voltage of left and right side of supercap current sense resistor and DC-DC output voltage
        printf ("%d.%04d, %d.%04d, %d.%04d, %d.%04d\r\n", wholePart2, decimalPart2, wholePart3, decimalPart3, wholePart4, decimalPart4, wholePart7, decimalPart7);
        Delay_Ms (500);
    }
}
