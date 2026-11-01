# Offline build of the original SCALE physics modules against six stub modules.
# `SCALE_ROOT` names a tree of scale-met/scale-dev at the commit README.md states; it is read only.
#   make SCALE_ROOT=<tree> [BUILD=<dir>]
ifndef SCALE_ROOT
$(error SCALE_ROOT is not set: pass the scale-dev tree, see README.md)
endif
BUILD ?= build
FC := gfortran
FFLAGS ?= -O2 -cpp -ffree-line-length-none
SELF := $(dir $(lastword $(MAKEFILE_LIST)))

COMMON := $(SCALE_ROOT)/scalelib/src/common
ATMOS := $(SCALE_ROOT)/scalelib/src/atmosphere/common
MP := $(SCALE_ROOT)/scalelib/src/atmosphere/physics/microphysics

# Link order is the `use` order.
SOURCES := \
  $(COMMON)/scale_precision.F90 \
  $(SELF)stubs/scale_io.F90 \
  $(SELF)stubs/scale_prc.F90 \
  $(SELF)stubs/scale_prof.F90 \
  $(SELF)stubs/scale_file_history.F90 \
  $(SELF)stubs/scale_tracer.F90 \
  $(SELF)stubs/scale_atmos_grid_cartesC_index.F90 \
  $(COMMON)/scale_const.F90 \
  $(ATMOS)/scale_atmos_hydrometeor.F90 \
  $(ATMOS)/scale_atmos_saturation.F90 \
  $(MP)/scale_atmos_phy_mp_common.F90 \
  $(MP)/scale_atmos_phy_mp_kessler.F90 \
  $(SELF)driver.F90

OBJECTS := $(addprefix $(BUILD)/,$(notdir $(SOURCES:.F90=.o)))

$(BUILD)/driver: $(OBJECTS)
	$(FC) $(FFLAGS) -o $@ $(OBJECTS)

# One serial rule per source: each object needs every module before it in `SOURCES`.
define COMPILE_RULE
$(BUILD)/$(notdir $(1:.F90=.o)): $(1) $(2) | $(BUILD)
	$(FC) $(FFLAGS) -I$(SCALE_ROOT)/scalelib/include -J$(BUILD) -c $(1) -o $$@
endef
PREVIOUS :=
$(foreach src,$(SOURCES),$(eval $(call COMPILE_RULE,$(src),$(PREVIOUS)))$(eval PREVIOUS += $(BUILD)/$(notdir $(src:.F90=.o))))

$(BUILD):
	mkdir -p $@

.PHONY: clean
clean:
	rm -rf $(BUILD)
