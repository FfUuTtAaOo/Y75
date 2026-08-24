#include "function.h"
#include "at24c04.h"
#include "main.h"
#include "tim.h"
#include "usart.h"
#include "filter.h"


/* Macro definition ---------------------------------------------------------------*/
#define TEST_NUM 30
#define VOLTAGE 5.0 / 3.3
#define MATRIX_SIZE 6
#define KILOGRAM_TO_NEWTON  9.8

/* Variable definition -------------------------------------------------------------*/
float matrix[MATRIX_SIZE][MATRIX_SIZE] = { {0.408874, -0.003628, -0.002742, 0.013313, 0.014064, -0.005611 },
										   {-0.007383, 0.407150, -0.007982, -0.012571, 0.002251, -0.001586 },
										   {0.001571, 0.000566, 0.687240, -0.000953, 0.003024, 0.018308 },
										   {-0.000200, -0.001137, 0.000052, 0.004020, -0.000873, -0.000017 },
										   {0.001290, -0.000250, -0.000088, 0.000932, 0.004019, -0.000033 },
										   {0.000121, 0.000292, -0.000158, 0.000193, 0.000010, 0.004928 }};
floatuint_t force[6] = { 0 };
float zero[6] = { 0 };
uint8_t matrix_count = 0;
uint8_t uartbuffer[UART_BUFFER_SIZE] = { 0 };
uint8_t at24c04_write_buffer[8] = { 0 };
appinfo_t app_info = { 0 };

/* External Variable references ---------------------------------------------------------*/
extern uint8_t debugMode;
extern uint8_t data_flag;
extern uint8_t rev_data[28];
extern uint8_t stop_flag;
extern uint8_t function_flag_second;


/* Function definition -------------------------------------------------------------*/
void dataProc(const float data[6], uint8_t ret[6])
{
    int i = 0;
    uint8_t tmp = 0, index = 0;

    if (data_flag == 0) {
        force[0].f32 = matrix[0][0] * data[0] / KILOGRAM_TO_NEWTON + matrix[0][1] * data[1] / KILOGRAM_TO_NEWTON +
                    matrix[0][2] * data[2] / KILOGRAM_TO_NEWTON + matrix[0][3] * data[3] / KILOGRAM_TO_NEWTON +
                    matrix[0][4] * data[4] / KILOGRAM_TO_NEWTON + matrix[0][5] * data[5] / KILOGRAM_TO_NEWTON;

        force[1].f32 = matrix[1][0] * data[0] / KILOGRAM_TO_NEWTON + matrix[1][1] * data[1] / KILOGRAM_TO_NEWTON +
                    matrix[1][2] * data[2] / KILOGRAM_TO_NEWTON + matrix[1][3] * data[3] / KILOGRAM_TO_NEWTON +
                    matrix[1][4] * data[4] / KILOGRAM_TO_NEWTON + matrix[1][5] * data[5] / KILOGRAM_TO_NEWTON;

        force[2].f32 = matrix[2][0] * data[0] / KILOGRAM_TO_NEWTON + matrix[2][1] * data[1] / KILOGRAM_TO_NEWTON +
                    matrix[2][2] * data[2] / KILOGRAM_TO_NEWTON + matrix[2][3] * data[3] / KILOGRAM_TO_NEWTON +
                    matrix[2][4] * data[4] / KILOGRAM_TO_NEWTON + matrix[2][5] * data[5] / KILOGRAM_TO_NEWTON;

        force[3].f32 = matrix[3][0] * data[0] / KILOGRAM_TO_NEWTON + matrix[3][1] * data[1] / KILOGRAM_TO_NEWTON +
                    matrix[3][2] * data[2] / KILOGRAM_TO_NEWTON + matrix[3][3] * data[3] / KILOGRAM_TO_NEWTON +
                    matrix[3][4] * data[4] / KILOGRAM_TO_NEWTON + matrix[3][5] * data[5] / KILOGRAM_TO_NEWTON;

        force[4].f32 = matrix[4][0] * data[0] / KILOGRAM_TO_NEWTON + matrix[4][1] * data[1] / KILOGRAM_TO_NEWTON +
                    matrix[4][2] * data[2] / KILOGRAM_TO_NEWTON + matrix[4][3] * data[3] / KILOGRAM_TO_NEWTON +
                    matrix[4][4] * data[4] / KILOGRAM_TO_NEWTON + matrix[4][5] * data[5] / KILOGRAM_TO_NEWTON;

        force[5].f32 = matrix[5][0] * data[0] / KILOGRAM_TO_NEWTON + matrix[5][1] * data[1] / KILOGRAM_TO_NEWTON +
                    matrix[5][2] * data[2] / KILOGRAM_TO_NEWTON + matrix[5][3] * data[3] / KILOGRAM_TO_NEWTON +
                    matrix[5][4] * data[4] / KILOGRAM_TO_NEWTON + matrix[5][5] * data[5] / KILOGRAM_TO_NEWTON;
    } else if (data_flag == 1) {
        force[0].f32 = data[0];
        force[1].f32 = data[1];
        force[2].f32 = data[2];
        force[3].f32 = data[3];
        force[4].f32 = data[4];
        force[5].f32 = data[5];
    } else if (data_flag == 2) {
        force[0].f32 = matrix[0][0] * data[0] + matrix[0][1] * data[1] +
                    matrix[0][2] * data[2] + matrix[0][3] * data[3] +
                    matrix[0][4] * data[4] + matrix[0][5] * data[5];

        force[1].f32 = matrix[1][0] * data[0] + matrix[1][1] * data[1] +
                    matrix[1][2] * data[2] + matrix[1][3] * data[3] +
                    matrix[1][4] * data[4] + matrix[1][5] * data[5];

        force[2].f32 = matrix[2][0] * data[0] + matrix[2][1] * data[1] +
                    matrix[2][2] * data[2] + matrix[2][3] * data[3] +
                    matrix[2][4] * data[4] + matrix[2][5] * data[5];

        force[3].f32 = matrix[3][0] * data[0] + matrix[3][1] * data[1] +
                    matrix[3][2] * data[2] + matrix[3][3] * data[3] +
                    matrix[3][4] * data[4] + matrix[3][5] * data[5];

        force[4].f32 = matrix[4][0] * data[0] + matrix[4][1] * data[1] +
                    matrix[4][2] * data[2] + matrix[4][3] * data[3] +
                    matrix[4][4] * data[4] + matrix[4][5] * data[5];

        force[5].f32 = matrix[5][0] * data[0] + matrix[5][1] * data[1] +
                    matrix[5][2] * data[2] + matrix[5][3] * data[3] +
                    matrix[5][4] * data[4] + matrix[5][5] * data[5];
    }

    for (i = 0; i < 6; i++) {
      force[i].f32 = FloatFilter_UpdateChannel(i, force[i].f32);
    }

    for (i = 3; i < 27; i++) {
        index = (i - 3) / 4;
        tmp = (i - 3) % 4;
        U32_TO_U8(force[index].u32, ret[i], tmp * 8);
    }
}

void send_ret(uint8_t command_code)
{
    float a[6] = {}, a_tmp;
    for (uint8_t i = 0; i < 6; ++i) {
        a_tmp = adcData() - zero[i];
        a[i] = a_tmp;
        a[i] *= VOLTAGE;
    }

    dataProc(a, rev_data);

    rev_data[0] = DATA_FRAMES_AA;
    rev_data[1] = DATA_FRAMES_55;
    rev_data[2] = command_code;
    rev_data[27] = 0x0D;
    rev_data[28] = 0x0A;

    if (debugMode == FALSE) {
        HAL_UART_Transmit(&huart1, rev_data, 29, 500);
    } else {
        printf("channels: %f,%f,%f,%f,%f,%f\r\n", force[0].f32, force[1].f32, force[2].f32, force[3].f32, force[4].f32, force[5].f32);
    }
}

void function_response(uint8_t data)
{
    int i = 0, j = 0, n = 0;
    floatuint_t matrix_data_tmp[6] = { 0 };
    rev_data[0] = DATA_FRAMES_AA;
    rev_data[1] = DATA_FRAMES_55;
    rev_data[2] = data;
    if (data == 0x33 || data == 0x35) {
        for (i = 0; i < 6; i++) {
            n = 0;
            for (j = 0; j < 6; j++) {
                matrix_data_tmp[j].f32 = matrix[i][j];
                if (matrix_data_tmp[j].u32 == 0xFFFFFFFF) {
                    n = 1;
                    break;
                }
            }
            if (n == 1)
                break;
        }
        if (i == 6 && j == 6) {
            rev_data[3] = 1;
        } else {
            rev_data[3] = 0;
        }
    } else {
        rev_data[3] = 1;
    }
    rev_data[4] = 0x0D;
    rev_data[5] = 0x0A;
    HAL_UART_Transmit(&huart1, rev_data, 6, 500);
}

void zero_clearing(void)
{
    float sum[6] = { 0 };
    float adc_data[TEST_NUM][6] = { 0 };
    uint8_t i = 0, j = 0;
    for (i = 0; i < TEST_NUM; i++) {
        HAL_Delay(20);
        for (j = 0; j < 6; j++) {
            adc_data[i][j] = adcData();
            sum[j] += adc_data[i][j];
        }
    }
    for (i = 0; i < 6; i++) {
        zero[i] = sum[i] / TEST_NUM;
    }
    return;
}

void matrix_data_processing(uint8_t index)
{
    int i = 0, j = 0, offset = 0, tmp = 0;
    uint8_t at24c04_write_buffer[32] = { 0 };
    floatuint_t matrix_data_tmp[6] = { 0 };
    for (i = 0; i < 24; i++) {
        at24c04_write_buffer[i] = uartbuffer[matrix_count - 25 + i];
    }
    switch (index) {
    case UPDATE_MATRIX_INDEX_0:
        at24c04_write(JZ_0_ADDR, at24c04_write_buffer, JZ_SIZE);
        break;
    case UPDATE_MATRIX_INDEX_1:
        at24c04_write(JZ_1_ADDR, at24c04_write_buffer, JZ_SIZE);
        break;
    case UPDATE_MATRIX_INDEX_2:
        at24c04_write(JZ_2_ADDR, at24c04_write_buffer, JZ_SIZE);
        break;
    case UPDATE_MATRIX_INDEX_3:
        at24c04_write(JZ_3_ADDR, at24c04_write_buffer, JZ_SIZE);
        break;
    case UPDATE_MATRIX_INDEX_4:
        at24c04_write(JZ_4_ADDR, at24c04_write_buffer, JZ_SIZE);
        break;
    case UPDATE_MATRIX_INDEX_5:
        at24c04_write(JZ_5_ADDR, at24c04_write_buffer, JZ_SIZE);
        break;
    }

//    for (i = 0; i < 6; i++) {
//        printf("write_buff:0x%X %X %X %X\r\n", at24c04_write_buffer[i * 4], at24c04_write_buffer[i * 4 + 1], at24c04_write_buffer[i * 4 + 2], at24c04_write_buffer[i * 4 + 3]);
//    }

    for (j = 0; j < 24; j++) {
        offset = j / 4;
        tmp = j % 4;
        matrix_data_tmp[offset].u32 = U8_TO_U32_SAFE(at24c04_write_buffer[j], matrix_data_tmp[offset].u32, 3 - tmp);
    }

    for (j = 0; j < 6; j++) {
        matrix[index][j] = matrix_data_tmp[j].f32;
        printf("%f ", matrix[index][j]);
    }
    printf("\r\n");
}

void sn_writing(void)
{
    uint8_t sn_buffer[SN_SIZE] = { 0 };
    for (uint8_t i = 0; i < SN_SIZE; ++i) {
        sn_buffer[i] = uartbuffer[matrix_count - SN_SIZE - 1 + i];
    }
    at24c04_write(SN_ADDR, sn_buffer, SN_SIZE);
    printf("SN updated: %s\r\n", sn_buffer);
}

void sn_reading(void)
{
    at24c04_read(SN_ADDR, app_info.sn, SN_SIZE);
    app_info.sn[SN_SIZE] = '\0';
    printf("SN: %s\r\n", app_info.sn);
}

void get_matrix_data(void)
{
    int i = 0, j = 0, offset = 0, tmp = 0;
    uint8_t at24c04_read_buffer[32] = { 0 };
    floatuint_t matrix_data_tmp[6] = { 0 };
    printf("matrix_data:\r\n");
    for (i = 0; i < MATRIX_SIZE; i++) {
        switch (i) {
        case UPDATE_MATRIX_INDEX_0:
            at24c04_read(JZ_0_ADDR, at24c04_read_buffer, JZ_SIZE);
            break;
        case UPDATE_MATRIX_INDEX_1:
            at24c04_read(JZ_1_ADDR, at24c04_read_buffer, JZ_SIZE);
            break;
        case UPDATE_MATRIX_INDEX_2:
            at24c04_read(JZ_2_ADDR, at24c04_read_buffer, JZ_SIZE);
            break;
        case UPDATE_MATRIX_INDEX_3:
            at24c04_read(JZ_3_ADDR, at24c04_read_buffer, JZ_SIZE);
            break;
        case UPDATE_MATRIX_INDEX_4:
            at24c04_read(JZ_4_ADDR, at24c04_read_buffer, JZ_SIZE);
            break;
        case UPDATE_MATRIX_INDEX_5:
            at24c04_read(JZ_5_ADDR, at24c04_read_buffer, JZ_SIZE);
            break;
        }

        for (j = 0; j < 24; j++) {
            offset = j / 4;
            tmp = j % 4;
            matrix_data_tmp[offset].u32 = U8_TO_U32_SAFE(at24c04_read_buffer[j], matrix_data_tmp[offset].u32, 3 - tmp);
        }
        for (j = 0; j < 6; j++) {
            matrix[i][j] = matrix_data_tmp[j].f32;
            printf("%f ", matrix[i][j]);
        }
        printf("\r\n");
    }
}

void continue_command(void)
{
//    ADC_DRDY_Init();
//    LHL_LHA7668_Start(&lha7668_ctx, LHA7668_MODE_CONT_CONV);

    while(1) {
        if (stop_flag == 1) {
            stop_flag = 0;
            HAL_TIM_Base_Stop_IT(&htim2);
            HAL_GPIO_WritePin(GPIOA, GPIO_PIN_12, GPIO_PIN_SET);
            break;
        } else {
            send_ret(GM_CONTINUE_COMMAND);
        }
    }
}

void once_command(void)
{
    send_ret(GM_ONCE_COMMAND);
}

void mod_baudrate(void)
{
    while(1) {
        if (function_flag_second == GM_BAUD_0x01) {
            uart1_bsp = UART_BAUD_0x01;
            break;
        } else if (function_flag_second == GM_BAUD_0x02) {
            uart1_bsp = UART_BAUD_0x02;
            break;
        } else if (function_flag_second == GM_BAUD_0x03) {
            uart1_bsp = UART_BAUD_0x03;
            break;
        } else if (function_flag_second == GM_BAUD_0x04) {
            uart1_bsp = UART_BAUD_0x04;
            break;
        } else if (function_flag_second == GM_BAUD_0x05) {
            uart1_bsp = UART_BAUD_0x05;
            break;
        }
    }
    for (int i = 0; i < 4; i++) {
        U32_TO_U8(uart1_bsp, at24c04_write_buffer[i], i * 8);
    }
    at24c04_write(UART_BAUD_ADDR, at24c04_write_buffer, 4);
    rev_data[0] = 'R';
    rev_data[1] = 'e';
    rev_data[2] = 's';
    rev_data[3] = 't';
    rev_data[4] = 'a';
    rev_data[5] = 'r';
    rev_data[6] = 't';
    rev_data[7] = ' ';
    rev_data[8] = 'T';
    rev_data[9] = 'h';
    rev_data[10] = 'e';
    rev_data[11] = ' ';
    rev_data[12] = 'S';
    rev_data[13] = 'y';
    rev_data[14] = 's';
    rev_data[15] = 't';
    rev_data[16] = 'e';
    rev_data[17] = 'm';
    rev_data[18] = '!';
    rev_data[19] = '\r';
    rev_data[20] = '\n';

    while(1) {
        HAL_UART_Transmit(&huart1, rev_data, 21, 500);
        HAL_Delay(1000);
    }
}

void APPINFO_Init(const uint8_t* ver, const uint8_t* date)
{
    if(ver != NULL) {
        strncpy((char*)app_info.version, (char*)ver, sizeof(app_info.version) - 1);
        app_info.version[sizeof(app_info.version) - 1] = '\0';
    }
    if(date != NULL) {
        strncpy((char*)app_info.build_date, (char*)date, sizeof(app_info.build_date) - 1);
        app_info.build_date[sizeof(app_info.build_date) - 1] = '\0';
    }
}

void ADC_DRDY_Init(void)
{
    GPIO_InitTypeDef GPIO_InitStruct = {0};

    GPIO_InitStruct.Pin = GPIO_PIN_6;
    GPIO_InitStruct.Mode = GPIO_MODE_IT_FALLING;
    GPIO_InitStruct.Pull = GPIO_NOPULL;
    HAL_GPIO_Init(GPIOA, &GPIO_InitStruct);

    HAL_NVIC_SetPriority(EXTI9_5_IRQn, 2, 0);
    HAL_NVIC_EnableIRQ(EXTI9_5_IRQn);
}


void SPI_MISO_Init(void)
{
    GPIO_InitTypeDef GPIO_InitStruct = {0};

    GPIO_InitStruct.Pin = GPIO_PIN_6;
    GPIO_InitStruct.Mode = GPIO_MODE_INPUT;
    GPIO_InitStruct.Pull = GPIO_NOPULL;
    HAL_GPIO_Init(GPIOA, &GPIO_InitStruct);
}


