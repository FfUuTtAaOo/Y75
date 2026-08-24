/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file           : main.h
  * @brief          : Header for main.c file.
  *                   This file contains the common defines of the application.
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

/* Define to prevent recursive inclusion -------------------------------------*/
#ifndef __MAIN_H
#define __MAIN_H

#ifdef __cplusplus
extern "C" {
#endif

/* Includes ------------------------------------------------------------------*/
#include "stm32f1xx_hal.h"

/* Private includes ----------------------------------------------------------*/
/* USER CODE BEGIN Includes */
#include <stdio.h>
#include <string.h>
#include <assert.h>

/* USER CODE END Includes */

/* Exported types ------------------------------------------------------------*/
/* USER CODE BEGIN ET */

/* USER CODE END ET */

/* Exported constants --------------------------------------------------------*/
/* USER CODE BEGIN EC */

/* USER CODE END EC */

/* Exported macro ------------------------------------------------------------*/
/* USER CODE BEGIN EM */

/* USER CODE END EM */

/* Exported functions prototypes ---------------------------------------------*/
void Error_Handler(void);

/* USER CODE BEGIN EFP */
void uart_debug(const char *msg);

/* USER CODE END EFP */

/* Private defines -----------------------------------------------------------*/
#define SPI1_CS_Pin GPIO_PIN_4
#define SPI1_CS_GPIO_Port GPIOA
#define ADC_DRDY_Pin GPIO_PIN_0
#define ADC_DRDY_GPIO_Port GPIOB
#define ADC_CLKIN_Pin GPIO_PIN_8
#define ADC_CLKIN_GPIO_Port GPIOA

/* USER CODE BEGIN Private defines */
#define DATA_FRAMES_AA         0xAA
#define DATA_FRAMES_55         0x55

#define GM_STOP_COMMAND        0x01
#define GM_CONTINUE_COMMAND    0x02
#define GM_ONCE_COMMAND        0x03
#define GM_MOD_BAUD            0x04
#define GM_BAUD_0x01          0x01
#define GM_BAUD_0x02          0x02
#define GM_BAUD_0x03          0x03
#define GM_BAUD_0x04          0x04
#define GM_BAUD_0x05          0x05
#define UART_BAUD_0x01        460800
#define UART_BAUD_0x02        256000
#define UART_BAUD_0x03        115200
#define UART_BAUD_0x04        19200
#define UART_BAUD_0x05        9600
#define GM_ADC_MODEL_INFO      0x05
#define GM_INIT_SEND_RET       0x06

#define USER_CLEAR_ZERO        0x30
#define EXIT_PRINT             0x31
#define GO_TO_PRINT            0x32
#define FILTERED_DATA          0x33
#define ORIGINAL_DATA          0x34
#define FILTERED_DATA_N        0x35
#define SWITCH_SAMPLING_RATE_0   0x36
#define SWITCH_SAMPLING_RATE_2   0x37

#define VERSION_INFORMATION    0x07

#define GM_FRAME_TAIL1            0x0D
#define GM_FRAME_TAIL2            0x0A

#define UPDATE_MATRIX_COMMAND       0x20
#define UPDATE_MATRIX_INDEX_0       0x00
#define UPDATE_MATRIX_INDEX_1       0x01
#define UPDATE_MATRIX_INDEX_2       0x02
#define UPDATE_MATRIX_INDEX_3       0x03
#define UPDATE_MATRIX_INDEX_4       0x04
#define UPDATE_MATRIX_INDEX_5       0x05
#define UART_BUFFER_SIZE 64

#define SN_COMMAND 0x21
#define SN_SIZE     16
#define SN_ADDR     0xB0

#define JZ_0_ADDR    0x00
#define JZ_1_ADDR    0x18
#define JZ_2_ADDR    0x30
#define JZ_3_ADDR    0x48
#define JZ_4_ADDR    0x60
#define JZ_5_ADDR    0x80
#define JZ_SIZE      24

#define U32_TO_U8(u32, u8, offset)  (u8) = (((u32) >> (offset)) & 0xFF)
#define UINT8_EQUAL(a, b) ((a) == (b))
#define U8_TO_U32_SAFE(u8_val, u32_val, index) \
  ({ \
    assert((index) < 4); \
    ((u32_val & ~(0xFFUL << ((index) * 8))) | (((uint32_t)(u8_val)) << ((index) * 8))); \
  })

#define TRUE 1
#define FALSE 0

typedef union {
    float f32;
    uint32_t u32;
} floatuint_t;

#pragma pack(push, 1)
typedef struct {
    uint8_t version[16];
    uint8_t build_date[24];
    uint8_t sn[SN_SIZE + 1];
    uint32_t crc32;
} appinfo_t;
#pragma pack(pop)

float adcData(void);

/* USER CODE END Private defines */

#ifdef __cplusplus
}
#endif

#endif /* __MAIN_H */
