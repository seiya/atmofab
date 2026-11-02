module scale_io
  implicit none
  private
  integer, public, parameter :: H_SHORT = 16, H_MID = 64, H_LONG = 256
  integer, public :: IO_FID_CONF = -1, IO_FID_LOG = 6, IO_FID_NML = -1
  logical, public :: IO_L = .false., IO_NML = .false.
  character(len=6), public :: IO_UNIVERSALRANK = "UNKNWN", IO_JOBID = "UNKNWN", IO_DOMAINID = "UNKNWN", IO_LOCALRANK = "UNKNWN"
end module scale_io
