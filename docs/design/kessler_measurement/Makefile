# Offline build of the original SCALE physics modules against six stub modules.
# `SCALE_ROOT` names a tree of scale-met/scale-dev at the commit README.md states; it is read only.
#   make SCALE_ROOT=<tree> [BUILD=<dir>]
ifndef SCALE_ROOT
$(error SCALE_ROOT is not set: pass the scale-dev tree, see README.md)
endif
BUILD ?= build
FC := gfortran
# gfortran's module-directory flag; ifort and ifx ignore -J and leave .mod files in the working
# directory, so they take MODFLAG="-module " and FFLAGS without -ffree-line-length-none.
MODFLAG ?= -J
FFLAGS ?= -O2 -cpp -ffree-line-length-none
SELF := $(dir $(lastword $(MAKEFILE_LIST)))

COMMON := $(SCALE_ROOT)/scalelib/src/common
ATMOS := $(SCALE_ROOT)/scalelib/src/atmosphere/common
MP := $(SCALE_ROOT)/scalelib/src/atmosphere/physics/microphysics

# `ALT_CONSTANTS=1` builds a copy of `scale_const` with the values of each suite's `alt_constants`,
# since the original holds them as compile-time parameters.
ifdef ALT_CONSTANTS
CONST_SOURCE := $(BUILD)/alt/scale_const.F90
else
CONST_SOURCE := $(COMMON)/scale_const.F90
endif

# Link order is the `use` order.
SOURCES := \
  $(COMMON)/scale_precision.F90 \
  $(SELF)stubs/scale_io.F90 \
  $(SELF)stubs/scale_prc.F90 \
  $(SELF)stubs/scale_prof.F90 \
  $(SELF)stubs/scale_file_history.F90 \
  $(SELF)stubs/scale_tracer.F90 \
  $(SELF)stubs/scale_atmos_grid_cartesC_index.F90 \
  $(CONST_SOURCE) \
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
	$(FC) $(FFLAGS) -I$(SCALE_ROOT)/scalelib/include $(MODFLAG)$(BUILD) -c $(1) -o $$@
endef
PREVIOUS :=
$(foreach src,$(SOURCES),$(eval $(call COMPILE_RULE,$(src),$(PREVIOUS)))$(eval PREVIOUS += $(BUILD)/$(notdir $(src:.F90=.o))))

$(BUILD)/alt/scale_const.F90: $(COMMON)/scale_const.F90 | $(BUILD)
	mkdir -p $(BUILD)/alt
	sed -E -e 's/(CONST_Rvap +=) +461.50_RP/\1 480.0_RP/' \
	  -e 's/(CONST_CPvap +=) +1846.00_RP/\1 1900.0_RP/' \
	  -e 's/(CONST_CL +=) +4218.0_RP/\1 4100.0_RP/' \
	  -e 's/(CONST_LHV0 +=) +2.501E\+6_RP/\1 2.4E+6_RP/' \
	  -e 's/(CONST_PSAT0 +=) +610.78_RP/\1 600.0_RP/' \
	  -e 's/(CONST_DWATR +=) +1000.0_RP/\1 950.0_RP/' \
	  -e 's/(CONST_TEM00 +=) +273.15_RP/\1 270.0_RP/' \
	  -e 's/CONST_EPS += +epsilon\(0.0_RP\)/CONST_EPS = 1.0E-15_RP/' $< > $@

$(BUILD):
	mkdir -p $@

.PHONY: clean
clean:
	rm -rf $(BUILD)
