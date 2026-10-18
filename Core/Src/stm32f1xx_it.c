/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file    stm32f1xx_it.c
  * @brief   Interrupt Service Routines.
  ******************************************************************************
  * @attention
  *
  * Copyright (c) 2025 STMicroelectronics.
  * All rights reserved.
  *
  * This software is licensed under terms that can be found in the LICENSE file
  * in the root directory of this software component.
  * If no LICENSE file comes with this software, it is provided AS-IS.
  *
  ******************************************************************************
  */
/* USER CODE END Header */

/* Includes ------------------------------------------------------------------*/
#include "main.h"
#include "stm32f1xx_it.h"
/* Private includes ----------------------------------------------------------*/
/* USER CODE BEGIN Includes */
#include "lha7668.h"

/* USER CODE END Includes */

/* Private typedef -----------------------------------------------------------*/
/* USER CODE BEGIN TD */

/* USER CODE END TD */

/* Private define ------------------------------------------------------------*/
/* USER CODE BEGIN PD */

/* USER CODE END PD */

/* Private macro -------------------------------------------------------------*/
/* USER CODE BEGIN PM */

/* USER CODE END PM */

/* Private variables ---------------------------------------------------------*/
/* USER CODE BEGIN PV */
uint32_t number = 0;
extern uint8_t function_flag_first;
extern uint8_t function_flag_second;
extern uint8_t function_flag_third;
extern uint8_t matrix_count;
extern uint8_t uartbuffer[UART_BUFFER_SIZE];
extern uint8_t stop_flag;
extern volatile uint32_t g_tick_ms;
extern lhl_lha7668_ctx_t lha7668_ctx;
/* 开机以来见过的最低 MSP，HardFault 转储会打印（判断栈余量） */
extern volatile uint32_t g_min_msp;

/* USER CODE END PV */

/* Private function prototypes -----------------------------------------------*/
/* USER CODE BEGIN PFP */

/* USER CODE END PFP */

/* Private user code ---------------------------------------------------------*/
/* USER CODE BEGIN 0 */

/* USER CODE END 0 */

/* External variables --------------------------------------------------------*/
extern TIM_HandleTypeDef htim2;
extern DMA_HandleTypeDef hdma_usart1_rx;
extern UART_HandleTypeDef huart1;
/* USER CODE BEGIN EV */
void EXTI9_5_IRQHandler()
{
    HAL_GPIO_EXTI_IRQHandler(GPIO_PIN_6);
}

void HAL_GPIO_EXTI_Callback(uint16_t GPIO_Pin)
{
    if(GPIO_Pin == GPIO_PIN_6){
    }
}

//float adc_get_data(void)
//{
//    while (LHL_LHA7668_Get_Flag(&lha7668_ctx, LHA7668_STATUS_RDY_FLAG) == LHA7668_SET);
//    LHL_LHA7668_Get_Data(&lha7668_ctx);
//    return LHL_LHA7668_Get_mVoltage(lha7668_ctx.data, LHA7668_BIPOLAR, LHA7668_PGA_X128, 2500);
//}

/* USER CODE END EV */

/******************************************************************************/
/*           Cortex-M3 Processor Interruption and Exception Handlers          */
/******************************************************************************/
/**
  * @brief This function handles Non maskable interrupt.
  */
void NMI_Handler(void)
{
  /* USER CODE BEGIN NonMaskableInt_IRQn 0 */

  /* USER CODE END NonMaskableInt_IRQn 0 */
  /* USER CODE BEGIN NonMaskableInt_IRQn 1 */
   while (1)
  {
  }
  /* USER CODE END NonMaskableInt_IRQn 1 */
}

/**
  * @brief This function handles Hard fault interrupt.
  */
// /* ------------------------------------------------------------------
//  *  HardFault 现场转储
//  *
//  *  一进异常就把「出错指令地址 + 访问地址 + 原因」从 USART1 打出来，
//  *  不用连调试器也能定位是哪一行代码、哪个指针炸的。
//  *  这里不调用 HAL / printf / SysTick（异常态下它们都不可靠），
//  *  直接轮询 USART1 的 TXE 位发字符。
//  * ------------------------------------------------------------------ */
// static void fault_putc(char c)
// {
//   uint32_t guard = 200000U;
//   while (((USART1->SR & USART_SR_TXE) == 0U) && (guard-- != 0U)) { }
//   USART1->DR = (uint16_t)(uint8_t)c;
// }

// static void fault_puts(const char *s)
// {
//   while (*s != '\0') fault_putc(*s++);
// }

// static void fault_puthex(uint32_t v)
// {
//   static const char hex[] = "0123456789ABCDEF";
//   int i;
//   fault_puts("0x");
//   for (i = 28; i >= 0; i -= 4) {
//     fault_putc(hex[(v >> (uint32_t)i) & 0xFU]);
//   }
// }

// static void fault_kv(const char *name, uint32_t v)
// {
//   fault_puts(name);
//   fault_puts(" = ");
//   fault_puthex(v);
//   fault_puts("\r\n");
// }

// /* CFSR 位 → 可读原因 */static void fault_decode(uint32_t cfsr)
// {
//   fault_puts("原因: ");
//   if (cfsr & (1UL << 0))  fault_puts("IACCVIOL(取指越权) ");
//   if (cfsr & (1UL << 1))  fault_puts("DACCVIOL(访问越权) ");
//   if (cfsr & (1UL << 3))  fault_puts("MUNSTKERR(异常返回解栈失败) ");
//   if (cfsr & (1UL << 4))  fault_puts("MSTKERR(入栈失败) ");
//   if (cfsr & (1UL << 7))  fault_puts("MMARVALID ");
//   if (cfsr & (1UL << 8))  fault_puts("IBUSERR(取指总线错误) ");
//   if (cfsr & (1UL << 9))  fault_puts("PRECISERR(精确数据访问错误) ");
//   if (cfsr & (1UL << 10)) fault_puts("IMPRECISERR(非精确数据访问错误) ");
//   if (cfsr & (1UL << 11)) fault_puts("UNSTKERR(返回解栈总线错误) ");
//   if (cfsr & (1UL << 12)) fault_puts("STKERR(入栈总线错误) ");
//   if (cfsr & (1UL << 15)) fault_puts("BFARVALID ");
//   if (cfsr & (1UL << 16)) fault_puts("UNDEFINSTR(未定义指令) ");
//   if (cfsr & (1UL << 17)) fault_puts("INVSTATE(非法状态/跳转到非Thumb地址) ");
//   if (cfsr & (1UL << 18)) fault_puts("INVPC(非法PC) ");
//   if (cfsr & (1UL << 19)) fault_puts("NOCP(协处理器不可用) ");
//   if (cfsr & (1UL << 24)) fault_puts("UNALIGNED(非对齐访问) ");
//   if (cfsr & (1UL << 25)) fault_puts("DIVBYZERO ");
//   fault_puts("\r\n");
// }

// /* 由 HardFault_Handler 的汇编尾巴调用：frame = 入栈的 R0-R3,R12,LR,PC,xPSR */
// void HardFault_Report(uint32_t *frame, uint32_t exc_return)
// {
//   uint32_t cfsr  = SCB->CFSR;
//   uint32_t hfsr  = SCB->HFSR;
//   uint32_t mmfar = SCB->MMFAR;
//   uint32_t bfar  = SCB->BFAR;
//   uint32_t msp   = __get_MSP();
//   uint32_t psp   = __get_PSP();
//   uint32_t i, w;

//   fault_puts("\r\n\r\n*** HardFault ***\r\n");
//   fault_kv("PC   ", frame[6]);          /* 出错的那条指令 */
//   fault_kv("LR   ", frame[5]);
//   fault_kv("xPSR ", frame[7]);
//   fault_kv("R0   ", frame[0]);
//   fault_kv("R1   ", frame[1]);
//   fault_kv("R2   ", frame[2]);
//   fault_kv("R3   ", frame[3]);
//   fault_kv("R12  ", frame[4]);
//   fault_kv("EXC_RET", exc_return);
//   fault_kv("CFSR ", cfsr);
//   fault_kv("HFSR ", hfsr);
//   fault_kv("MMFAR", mmfar);
//   fault_kv("BFAR ", bfar);              /* 真正访问失败的那个地址 */
//   fault_decode(cfsr);
//   fault_kv("MSP  ", msp);
//   fault_kv("PSP  ", psp);
//   fault_kv("最低MSP", g_min_msp);       /* 开机以来栈最深到过哪里 */

//   /* 栈里最近的 12 个字（异常现场附近），帮助判断是不是帧被写坏 */
//   fault_puts("栈顶 12 字: ");
//   for (i = 0; i < 12; i++) {
//     uint32_t a = msp + i * 4U;
//     if (a + 4U > 0x20005000UL) break;   /* 不许读越界，否则在异常里再异常 */
//     w = *(volatile uint32_t *)a;
//     fault_puthex(w);
//     fault_putc(' ');
//   }
//   fault_puts("\r\n");

//   /* 栈里的返回地址候选（指向 0x08000000~0x08020000 的字），
//      从最近往里扫，就是一条不用调试器的调用链 */
//   fault_puts("栈内返回地址: ");
//   for (i = 0; i < 48; i++) {
//     uint32_t a = msp + i * 4U;
//     if (a + 4U > 0x20005000UL) break;
//     w = *(volatile uint32_t *)a;
//     if (w >= 0x08000000UL && w < 0x08020000UL) {
//       fault_puthex(w & ~1UL);
//       fault_putc(' ');
//     }
//   }
//   fault_puts("\r\n");

//   for (;;) { }                          /* 停在这里，等复位 */
// }

__attribute__((naked)) void HardFault_Handler(void)
{
//   __asm volatile(
//       "tst   lr, #4            \n"   /* EXC_RETURN bit2: 0=MSP 1=PSP */
//       "ite   eq                \n"
//       "mrseq r0, msp           \n"
//       "mrsne r0, psp           \n"
//       "mov   r1, lr            \n"
//       "b     HardFault_Report  \n");
}

/**
  * @brief This function handles Memory management fault.
  */
void MemManage_Handler(void)
{
  /* USER CODE BEGIN MemoryManagement_IRQn 0 */

  /* USER CODE END MemoryManagement_IRQn 0 */
  while (1)
  {
    /* USER CODE BEGIN W1_MemoryManagement_IRQn 0 */
    /* USER CODE END W1_MemoryManagement_IRQn 0 */
  }
}

/**
  * @brief This function handles Prefetch fault, memory access fault.
  */
void BusFault_Handler(void)
{
  /* USER CODE BEGIN BusFault_IRQn 0 */

  /* USER CODE END BusFault_IRQn 0 */
  while (1)
  {
    /* USER CODE BEGIN W1_BusFault_IRQn 0 */
    /* USER CODE END W1_BusFault_IRQn 0 */
  }
}

/**
  * @brief This function handles Undefined instruction or illegal state.
  */
void UsageFault_Handler(void)
{
  /* USER CODE BEGIN UsageFault_IRQn 0 */

  /* USER CODE END UsageFault_IRQn 0 */
  while (1)
  {
    /* USER CODE BEGIN W1_UsageFault_IRQn 0 */
    /* USER CODE END W1_UsageFault_IRQn 0 */
  }
}

/**
  * @brief This function handles System service call via SWI instruction.
  */
void SVC_Handler(void)
{
  /* USER CODE BEGIN SVCall_IRQn 0 */

  /* USER CODE END SVCall_IRQn 0 */
  /* USER CODE BEGIN SVCall_IRQn 1 */

  /* USER CODE END SVCall_IRQn 1 */
}

/**
  * @brief This function handles Debug monitor.
  */
void DebugMon_Handler(void)
{
  /* USER CODE BEGIN DebugMonitor_IRQn 0 */

  /* USER CODE END DebugMonitor_IRQn 0 */
  /* USER CODE BEGIN DebugMonitor_IRQn 1 */

  /* USER CODE END DebugMonitor_IRQn 1 */
}

/**
  * @brief This function handles Pendable request for system service.
  */
void PendSV_Handler(void)
{
  /* USER CODE BEGIN PendSV_IRQn 0 */

  /* USER CODE END PendSV_IRQn 0 */
  /* USER CODE BEGIN PendSV_IRQn 1 */

  /* USER CODE END PendSV_IRQn 1 */
}

/**
  * @brief This function handles System tick timer.
  */
void SysTick_Handler(void)
{
  /* USER CODE BEGIN SysTick_IRQn 0 */

  /* USER CODE END SysTick_IRQn 0 */
  HAL_IncTick();
  /* USER CODE BEGIN SysTick_IRQn 1 */

  /* USER CODE END SysTick_IRQn 1 */
}

/******************************************************************************/
/* STM32F1xx Peripheral Interrupt Handlers                                    */
/* Add here the Interrupt Handlers for the used peripherals.                  */
/* For the available peripheral interrupt handler names,                      */
/* please refer to the startup file (startup_stm32f1xx.s).                    */
/******************************************************************************/

/**
  * @brief This function handles DMA1 channel5 global interrupt.
  */
void DMA1_Channel5_IRQHandler(void)
{
  /* USER CODE BEGIN DMA1_Channel5_IRQn 0 */

  /* USER CODE END DMA1_Channel5_IRQn 0 */
  HAL_DMA_IRQHandler(&hdma_usart1_rx);
  /* USER CODE BEGIN DMA1_Channel5_IRQn 1 */

  /* USER CODE END DMA1_Channel5_IRQn 1 */
}

/**
  * @brief This function handles TIM2 global interrupt.
  */
void TIM2_IRQHandler(void)
{
  /* USER CODE BEGIN TIM2_IRQn 0 */

  /* USER CODE END TIM2_IRQn 0 */
  HAL_TIM_IRQHandler(&htim2);
  /* USER CODE BEGIN TIM2_IRQn 1 */

    // if (g_tick_ms % 500 == 0) {
    //     HAL_GPIO_TogglePin(GPIOA, GPIO_PIN_12);
    //     g_tick_ms = 0;
    // }
    // g_tick_ms++;

  /* USER CODE END TIM2_IRQn 1 */
}


/* USER CODE BEGIN 1 */

/* USER CODE END 1 */
