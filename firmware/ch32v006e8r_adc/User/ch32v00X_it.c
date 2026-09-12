/********************************** (C) COPYRIGHT *******************************
 * File Name          : ch32v00X_it.c
 * Author             : WCH
 * Version            : V1.0.0
 * Date               : 2024/11/04
 * Description        : Main Interrupt Service Routines.
*********************************************************************************
* Copyright (c) 2021 Nanjing Qinheng Microelectronics Co., Ltd.
* Attention: This software (modified or not) and binary are used for 
* microcontroller manufactured by Nanjing Qinheng Microelectronics.
*******************************************************************************/
#include <ch32v00X_it.h>

void adc_rx_push(uint8_t b);

void NMI_Handler(void) __attribute__((interrupt("WCH-Interrupt-fast")));
void HardFault_Handler(void) __attribute__((interrupt("WCH-Interrupt-fast")));

/*********************************************************************
 * @fn      NMI_Handler
 *
 * @brief   This function handles NMI exception.
 *
 * @return  none
 */
void NMI_Handler(void)
{
  while (1)
  {
  }
}

/*********************************************************************
 * @fn      HardFault_Handler
 *
 * @brief   This function handles Hard Fault exception.
 *
 * @return  none
 */
void HardFault_Handler(void)
{
  NVIC_SystemReset();
  while (1)
  {
  }
}

/*********************************************************************
 * @fn      USART1_IRQHandler
 *
 * @brief   USART1 RX interrupt. Pushes received bytes into the ring buffer.
 *
 * @return  none
 */
void USART1_IRQHandler(void) __attribute__((interrupt("WCH-Interrupt-fast")));
void USART1_IRQHandler(void)
{
  if (USART_GetFlagStatus(USART1, USART_FLAG_RXNE) != RESET)
  {
    adc_rx_push((uint8_t)USART_ReceiveData(USART1));
  }

  /* Clear overrun / framing / noise error flags so RX does not lock up. */
  if ((USART_GetFlagStatus(USART1, USART_FLAG_ORE) != RESET) ||
      (USART_GetFlagStatus(USART1, USART_FLAG_NE) != RESET) ||
      (USART_GetFlagStatus(USART1, USART_FLAG_FE) != RESET))
  {
    (void)USART_ReceiveData(USART1);
    USART1->STATR &= ~(USART_STATR_FE | USART_STATR_NE | USART_STATR_ORE);
  }
}


