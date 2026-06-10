MODULE_TOPDIR = $(shell grass --config path)

PGM = v.in.nhdplus

include $(MODULE_TOPDIR)/include/Make/Script.make

default: script
