#ifndef __FUNCTION_H__
#define __FUNCTION_H__

#include "main.h"

#define UART_BAUD_ADDR      0x78

void dataProc(const float data[6], uint8_t ret[6]);
void send_ret(uint8_t command_code);
void zero_clearing(void);
void matrix_data_processing(uint8_t index);
void get_matrix_data(void);
void continue_command(void);
void once_command(void);
void mod_baudrate(void);
void sn_writing(void);
void sn_reading(void);

void ADC_DRDY_Init(void);
void SPI_MISO_Init(void);

void APPINFO_Init(const uint8_t* ver, const uint8_t* date);

void function_response(uint8_t data);

HAL_StatusTypeDef APPINFO_SaveToEEPROM(void);

HAL_StatusTypeDef APPINFO_LoadFromEEPROM(void);

uint8_t APPINFO_CheckAndUpdateOnBoot(void);

#endif
