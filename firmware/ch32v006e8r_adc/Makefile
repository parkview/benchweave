# Makefile for the BenchWeave ADC board (CH32V006E8R) firmware.
# Standalone CLI build using the MRS-bundled riscv-wch-elf-gcc toolchain.
# No MounRiver Studio IDE required.

# ── Toolchain (spaces in path MUST be backslash-escaped) ──────────────────
MRS_BASE  := /usr/share/MRS2/MRS-linux-x64/resources/app/resources/linux
TCHAIN    := $(MRS_BASE)/components/WCH/Toolchain/RISC-V\ Embedded\ GCC12/bin

CC        := $(TCHAIN)/riscv-wch-elf-gcc
OBJCOPY   := $(TCHAIN)/riscv-wch-elf-objcopy
OBJDUMP   := $(TCHAIN)/riscv-wch-elf-objdump
SIZE      := $(TCHAIN)/riscv-wch-elf-size

# ── Project name ───────────────────────────────────────────────────────────
PROJECT   := ch32v006e8r_adc

# ── Directories ────────────────────────────────────────────────────────────
BUILD_DIR := build
TARGET    := $(BUILD_DIR)/$(PROJECT)

# ── Source files ───────────────────────────────────────────────────────────
C_SRCS    := $(wildcard User/*.c Debug/*.c Core/*.c Peripheral/src/*.c)
S_SRCS    := $(wildcard Startup/*.S)

C_OBJS    := $(C_SRCS:%.c=$(BUILD_DIR)/%.o)
S_OBJS    := $(S_SRCS:%.S=$(BUILD_DIR)/%.o)
ALL_OBJS  := $(C_OBJS) $(S_OBJS)

# ── Flags ──────────────────────────────────────────────────────────────────
ARCH_FLAGS   := -march=rv32ec_zmmul_xw -mabi=ilp32e \
                -msmall-data-limit=0 -msave-restore
COMMON_FLAGS := $(ARCH_FLAGS) -fmax-errors=20 -Os -fmessage-length=0 \
                -fsigned-char -ffunction-sections -fdata-sections \
                -fno-common -Wunused -Wuninitialized -g

INCLUDES     := -I Debug -I Core -I User -I Peripheral/inc

CFLAGS       := $(COMMON_FLAGS) $(INCLUDES) -std=gnu99 -MMD -MP
AFLAGS       := $(COMMON_FLAGS) -I Startup -x assembler-with-cpp -MMD -MP

LDFLAGS      := $(COMMON_FLAGS) \
                -T Ld/Link.ld \
                -nostartfiles \
                -Xlinker --gc-sections \
                -Wl,-Map,$(TARGET).map \
                --specs=nano.specs \
                --specs=nosys.specs

LIBS         := -lprintf

# ── Build rules ────────────────────────────────────────────────────────────
.PHONY: all clean flash

all: $(TARGET).elf $(TARGET).hex $(TARGET).lst $(TARGET).siz

$(TARGET).elf: $(ALL_OBJS)
	@mkdir -p $(BUILD_DIR)
	@echo "  LINK    $@"
	@$(CC) $(LDFLAGS) -o $@ $(ALL_OBJS) $(LIBS)

$(TARGET).hex: $(TARGET).elf
	@echo "  HEX     $@"
	@$(OBJCOPY) -O ihex $< $@

$(TARGET).lst: $(TARGET).elf
	@echo "  LST     $@"
	@$(OBJDUMP) --all-headers --demangle --disassemble -M xw $< > $@

$(TARGET).siz: $(TARGET).elf
	@echo "  SIZE    $@"
	@$(SIZE) --format=berkeley $< | tee $@

$(BUILD_DIR)/%.o: %.c
	@mkdir -p $(dir $@)
	@echo "  CC      $<"
	@$(CC) $(CFLAGS) -MF $(@:.o=.d) -MT $@ -c -o $@ $<

$(BUILD_DIR)/%.o: %.S
	@mkdir -p $(dir $@)
	@echo "  AS      $<"
	@$(CC) $(AFLAGS) -MF $(@:.o=.d) -MT $@ -c -o $@ $<

clean:
	rm -rf $(BUILD_DIR)

# ── Dependency tracking ────────────────────────────────────────────────────
DEPS := $(C_SRCS:%.c=$(BUILD_DIR)/%.d) $(S_SRCS:%.S=$(BUILD_DIR)/%.d)
-include $(DEPS)
