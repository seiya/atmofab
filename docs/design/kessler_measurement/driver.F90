! Runs one column through the original SCALE procedures. stdin: `<operation> <ka> <ks> <ke> <dt>`,
! then one `<name> <ka values>` line per input variable. stdout: `<name> <ka values>`, one per
! variable the operation returns, each value to 18 significant digits.
!
! `intent(out)` arrays start at 0, and SCALE writes interior cells only, so a halo cell of an
! output is 0 here as the Controlled Spec defines it. `guard_pass` and `converged` are not SCALE
! outputs: a run that reaches its end did not abort, which is what they state on a pass case.
program kessler_measurement_driver
  use scale_precision
  use scale_io, only: IO_FID_CONF
  use scale_const, only: CONST_setup, CONST_EPS, CONST_DWATR, CONST_Rvap, CONST_CPvap, CONST_CL, &
       CONST_LHV0, CONST_PSAT0, CONST_TEM00
  use scale_file_history, only: captured
  use scale_atmos_hydrometeor, only: LHV
  use scale_atmos_saturation, only: ATMOS_SATURATION_setup, ATMOS_SATURATION_dens2qsat_liq, &
       ATMOS_SATURATION_moist_conversion_dens_liq
  use scale_atmos_phy_mp_common, only: ATMOS_PHY_MP_saturation_adjustment
  use scale_atmos_phy_mp_kessler, only: ATMOS_PHY_MP_kessler_adjustment, &
       ATMOS_PHY_MP_kessler_terminal_velocity
  implicit none

  character(len=32) :: operation, name
  integer :: ka, ks, ke, k, ios
  real(DP) :: dt
  real(RP), allocatable, dimension(:) :: temp, dens, pres, qv, qc, qr, cptot, cvtot, &
       rhoq_c, rhoq_r, refstate_dens, tmp

  open(newunit=IO_FID_CONF, status='scratch', form='formatted')
  call CONST_setup
  call ATMOS_SATURATION_setup

  read(*,*) operation, ka, ks, ke, dt
  allocate(temp(ka), dens(ka), pres(ka), qv(ka), qc(ka), qr(ka), cptot(ka), cvtot(ka), &
       rhoq_c(ka), rhoq_r(ka), refstate_dens(ka), tmp(ka))
  do
     read(*,*,iostat=ios) name, tmp
     if (ios /= 0) exit
     select case (trim(name))
     case ('temp'); temp = tmp
     case ('dens'); dens = tmp
     case ('pres'); pres = tmp
     case ('qv'); qv = tmp
     case ('qc'); qc = tmp
     case ('qr'); qr = tmp
     case ('cptot'); cptot = tmp
     case ('cvtot'); cvtot = tmp
     case ('rhoq_c'); rhoq_c = tmp
     case ('rhoq_r'); rhoq_r = tmp
     case ('refstate_dens'); refstate_dens = tmp
     case default; error stop 'unknown input variable'
     end select
  end do

  select case (trim(operation))
  case ('constants')
     write(*,'(A,ES26.17E3)') 'CONST_EPS ', CONST_EPS, 'CONST_DWATR ', CONST_DWATR, &
          'CONST_Rvap ', CONST_Rvap, 'CONST_CPvap ', CONST_CPvap, 'CONST_CL ', CONST_CL, &
          'CONST_LHV0 ', CONST_LHV0, 'CONST_PSAT0 ', CONST_PSAT0, 'CONST_TEM00 ', CONST_TEM00, &
          'LHV00 ', LHV
  case ('dens2qsat_liq')
     call run_qsat
  case ('saturation_adjustment')
     call run_saturation_adjustment
  case ('moist_conversion')
     call run_moist_conversion
  case ('adjustment')
     call run_adjustment
  case ('terminal_velocity')
     call run_terminal_velocity
  case default
     error stop 'unknown operation'
  end select

contains

  subroutine emit(label, a)
    character(len=*), intent(in) :: label
    real(RP), intent(in) :: a(:)
    write(*,'(A)',advance='no') label
    write(*,'(*(1X,ES26.17E3))') a
  end subroutine emit

  subroutine run_qsat
    real(RP) :: qsat(ka)
    qsat = 0.0_RP
    do k = ks, ke
       call ATMOS_SATURATION_dens2qsat_liq(temp(k), dens(k), qsat(k))
    end do
    call emit('qsat', qsat)
  end subroutine run_qsat

  subroutine run_saturation_adjustment
    real(RP) :: t3(ka,1,1), d3(ka,1,1), v3(ka,1,1), c3(ka,1,1), i3(ka,1,1), p3(ka,1,1), w3(ka,1,1), &
         e3(ka,1,1)
    d3(:,1,1) = dens; t3(:,1,1) = temp; v3(:,1,1) = qv; c3(:,1,1) = qc
    i3 = -1.0_RP; p3(:,1,1) = cptot; w3(:,1,1) = cvtot; e3 = 0.0_RP
    call ATMOS_PHY_MP_saturation_adjustment(ka, ks, ke, 1, 1, 1, 1, 1, 1, d3, .true., &
         t3, v3, c3, i3, p3, w3, e3)
    call emit('temp', t3(:,1,1)); call emit('qv', v3(:,1,1)); call emit('qc', c3(:,1,1))
    call emit('cptot', p3(:,1,1)); call emit('cvtot', w3(:,1,1)); call emit('rhoe_d', e3(:,1,1))
  end subroutine run_saturation_adjustment

  ! One `converged` per cell, with no abort: what the abort of the procedure above hides.
  subroutine run_moist_conversion
    real(RP) :: result(ka), tl, vl, cl, pl, wl, em
    logical :: conv
    result = 1.0_RP
    do k = ks, ke
       tl = temp(k); vl = qv(k); cl = qc(k); pl = cptot(k); wl = cvtot(k)
       em = tl*wl + vl*LHV
       call ATMOS_SATURATION_moist_conversion_dens_liq(dens(k), em, tl, vl, cl, pl, wl, conv)
       result(k) = merge(1.0_RP, 0.0_RP, conv)
    end do
    call emit('converged_per_cell', result)
  end subroutine run_moist_conversion

  subroutine run_adjustment
    real(RP) :: d3(ka,1,1), p3(ka,1,1), t3(ka,1,1), q4(ka,1,1,3), cp3(ka,1,1), cv3(ka,1,1), &
         r3(ka,1,1), e3(ka,1,1), pc(ka)
    d3(:,1,1) = dens; p3(:,1,1) = pres; t3(:,1,1) = temp
    q4(:,1,1,1) = qv; q4(:,1,1,2) = qc; q4(:,1,1,3) = qr
    cp3(:,1,1) = cptot; cv3(:,1,1) = cvtot; r3 = 0.0_RP; e3 = 0.0_RP
    call ATMOS_PHY_MP_kessler_adjustment(ka, ks, ke, 1, 1, 1, 1, 1, 1, d3, p3, dt, &
         t3, q4, cp3, cv3, r3, e3)
    pc = 0.0_RP
    pc(ks:ke) = captured(ks:ke,1,1)
    call emit('temp', t3(:,1,1)); call emit('qv', q4(:,1,1,1)); call emit('qc', q4(:,1,1,2))
    call emit('qr', q4(:,1,1,3)); call emit('cptot', cp3(:,1,1)); call emit('cvtot', cv3(:,1,1))
    call emit('rhoe_t', r3(:,1,1)); call emit('evaporate', e3(:,1,1)); call emit('pcsat', pc)
  end subroutine run_adjustment

  subroutine run_terminal_velocity
    real(RP) :: rhoq(ka,2), vterm(ka,2)
    rhoq(:,1) = rhoq_c; rhoq(:,2) = rhoq_r; vterm = 0.0_RP
    call ATMOS_PHY_MP_kessler_terminal_velocity(ka, ks, ke, dens, rhoq, refstate_dens, vterm)
    call emit('vterm_c', vterm(:,1)); call emit('vterm_r', vterm(:,2))
  end subroutine run_terminal_velocity

end program kessler_measurement_driver
