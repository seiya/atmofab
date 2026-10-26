module shallow_water2d_channel_model
  use, intrinsic :: iso_fortran_env, only: dp => real64
  use dynamics_shallow_water_boundary_2d_channel_mirror_model, only: &
    dynamics_shallow_water_boundary_2d_channel_mirror__apply
  use dynamics_shallow_water_flux_2d_rusanov_model, only: &
    dynamics_shallow_water_flux_2d_rusanov__compute_flux
  use dynamics_shallow_water_reconstruction_2d_muscl_mc_model, only: &
    dynamics_shallow_water_reconstruction_2d_muscl_mc__reconstruct
  use dynamics_shallow_water_source_2d_coriolis_model, only: &
    dynamics_shallow_water_source_2d_coriolis__apply
  use dynamics_shallow_water_source_2d_tc4_forcing_model, only: &
    dynamics_shallow_water_source_2d_tc4_forcing__apply
  use dynamics_shallow_water_time_update_2d_rk4_model, only: &
    dynamics_shallow_water_time_update_2d_rk4__advance
  implicit none
  private
  public :: channel_config, prepare, reference_state, shallow_water2d_channel__integrate
  type :: channel_config
    integer :: nx, ny, ng, axis, profile, sign, planned
    real(dp) :: lx, ly, grav, omega, radius, theta, gh0, eta, shift, jet
    real(dp) :: start, finish, scale, target, dx, dy, ls, lr, sigma, f0, beta, u0, dt
    real(dp) :: psi0, rlow, ctr, sc0, rc
    real(dp), allocatable :: x(:), y(:), f(:,:), dfdr(:,:), ubar(:,:), dubar(:,:)
    real(dp), allocatable :: hbar(:,:)
  end type channel_config
contains
  real(dp) function jet_velocity(p,r) result(v)
    type(channel_config), intent(in) :: p
    real(dp), intent(in) :: r
    real(dp) :: z
    z=0.3_dp*(r-p%lr/8.0_dp)/(0.75_dp*p%lr)
    v=0.0_dp
    if (z>0.0_dp .and. z<0.3_dp) then
      v=p%u0*exp(-1.0_dp/z-1.0_dp/(0.3_dp-z)+4.0_dp/0.3_dp)
    end if
  end function jet_velocity

  real(dp) function primitive(p,r) result(v)
    type(channel_config), intent(in) :: p
    real(dp), intent(in) :: r
    real(dp) :: k
    k=acos(-1.0_dp)/p%lr
    v=p%f0*(r/2.0_dp-sin(2.0_dp*k*r)/(4.0_dp*k)) &
      +p%beta*(r*r/4.0_dp-p%lr*r/4.0_dp &
      -(r-p%lr/2.0_dp)*sin(2.0_dp*k*r)/(4.0_dp*k) &
      -(cos(2.0_dp*k*r)-1.0_dp)/(8.0_dp*k*k))
  end function primitive

  subroutine prepare(p)
    type(channel_config), intent(inout) :: p
    integer :: i,j,m,nr,q
    real(dp) :: r,dr,k,integ,rq,weight,us,hb,pi
    pi=acos(-1.0_dp)
    if (p%axis/=1 .and. p%axis/=2) error stop 'invalid channel axis'
    if (abs(p%sign)/=1) error stop 'invalid zonal sign'
    if (p%omega==0.0_dp) error stop 'zero rotation rate'
    if (abs(p%theta)<=0.0_dp .or. abs(p%theta)>=pi/2.0_dp) &
      error stop 'invalid reference latitude'
    select case(p%profile)
    case(2,3)
      continue
    case(21)
      if (p%eta<=0.0_dp .or. p%shift<0.0_dp .or. p%shift>=1.0_dp) &
        error stop 'invalid perturbation parameters'
    case(4)
      if (p%jet<=0.0_dp) error stop 'invalid jet maximum'
    case default
      error stop 'invalid flow profile'
    end select
    p%dx=p%lx/p%nx
    p%dy=p%ly/p%ny
    p%ls=p%lx
    p%lr=p%ly
    dr=p%dy
    nr=p%ny
    p%sigma=1.0_dp
    if (p%axis==2) then
      p%ls=p%ly
      p%lr=p%lx
      dr=p%dx
      nr=p%nx
      p%sigma=-1.0_dp
    end if
    p%f0=2.0_dp*p%omega*sin(p%theta)
    p%beta=2.0_dp*p%omega*cos(p%theta)/p%radius
    p%u0=p%sign*2.0_dp*pi*p%radius/(12.0_dp*86400.0_dp)
    p%psi0=-0.03_dp*p%gh0/p%f0
    p%rlow=2.0_dp*p%radius/12.74244_dp
    p%ctr=p%sign*p%jet*cos(p%theta)
    p%sc0=p%ls/2.0_dp
    p%rc=p%lr/2.0_dp
    allocate(p%x(p%nx),p%y(p%ny),p%f(p%nx,p%ny),p%dfdr(p%nx,p%ny))
    allocate(p%ubar(p%nx,p%ny),p%dubar(p%nx,p%ny),p%hbar(p%nx,p%ny))
    p%x=[( (i-0.5_dp)*p%dx,i=1,p%nx)]
    p%y=[( (j-0.5_dp)*p%dy,j=1,p%ny)]
    k=pi/p%lr
    !$omp parallel do private(r,us,hb,integ,q,rq,weight,i,j) schedule(static)
    do m=1,nr
      r=(m-0.5_dp)*dr
      us=p%u0
      hb=p%gh0/p%grav-p%sigma*p%u0/p%grav &
        *(p%f0*(r-p%lr/2.0_dp)+p%beta/2.0_dp*(r-p%lr/2.0_dp)**2)
      if (p%profile==3) then
        us=jet_velocity(p,r)
        integ=0.0_dp
        do q=0,64*m-32
          rq=q*dr/64.0_dp
          weight=2.0_dp
          if (mod(q,2)==1) weight=4.0_dp
          if (q==0 .or. q==64*m-32) weight=1.0_dp
          integ=integ+weight*(p%f0+p%beta*(rq-p%lr/2.0_dp))*jet_velocity(p,rq)
        end do
        hb=p%gh0/p%grav-p%sigma/p%grav*integ*dr/192.0_dp
      else if (p%profile==4) then
        us=p%sign*p%jet*sin(k*r)**2
        hb=p%gh0/p%grav-p%sigma*p%sign*p%jet/p%grav &
          *(primitive(p,r)-primitive(p,p%lr/2.0_dp))
      end if
      do q=1,merge(p%nx,p%ny,p%axis==1)
        i=q
        j=m
        if (p%axis==2) then
          i=m
          j=q
        end if
        p%f(i,j)=p%f0+p%beta*(r-p%lr/2.0_dp)
        p%dfdr(i,j)=p%beta
        p%ubar(i,j)=us
        p%dubar(i,j)=p%sign*p%jet*k*sin(2.0_dp*k*r)
        p%hbar(i,j)=hb
      end do
    end do
    !$omp end parallel do
  end subroutine prepare

  subroutine reference_state(p,t,state)
    type(channel_config), intent(in) :: p
    real(dp), intent(in) :: t
    real(dp), intent(out) :: state(3,p%nx,p%ny)
    integer :: i,j
    real(dp) :: along,cross,psi,ps,pr,hh,uu,vv,xi,pi
    pi=acos(-1.0_dp)
    !$omp parallel do collapse(2) private(along,cross,psi,ps,pr,hh,uu,vv,xi) &
    !$omp& schedule(static)
    do j=1,p%ny
      do i=1,p%nx
        along=p%x(i)
        cross=p%y(j)
        if (p%axis==2) then
          along=p%y(j)
          cross=p%x(i)
        end if
        hh=p%hbar(i,j)
        uu=0.0_dp
        vv=0.0_dp
        if (p%axis==1) uu=p%ubar(i,j)
        if (p%axis==2) vv=p%ubar(i,j)
        if (p%profile==21) hh=hh+p%eta*sin(2.0_dp*pi*(along/p%ls-p%shift))
        if (p%profile==4) then
          xi=pi*(along-p%sc0-p%ctr*t)/p%ls
          psi=p%psi0*exp(-((p%ls/pi*sin(xi))**2+(cross-p%rc)**2)/p%rlow**2)
          ps=-p%ls/(pi*p%rlow**2)*sin(2.0_dp*xi)*psi
          pr=-2.0_dp*(cross-p%rc)/p%rlow**2*psi
          hh=hh+p%f(i,j)*psi/p%grav
          if (p%axis==1) then
            uu=uu-pr
            vv=vv+ps
          else
            uu=uu-ps
            vv=vv+pr
          end if
        end if
        state(:,i,j)=[hh,hh*uu,hh*vv]
      end do
    end do
    !$omp end parallel do
  end subroutine reference_state

  subroutine shallow_water2d_channel__integrate(p,h_in,hu_in,hv_in,n_in, &
      h_out,hu_out,hv_out,n_out,min_depth,max_cfl,ok)
    type(channel_config), intent(in) :: p
    real(dp), intent(in) :: h_in(p%nx,p%ny),hu_in(p%nx,p%ny),hv_in(p%nx,p%ny),n_in
    real(dp), intent(out) :: h_out(p%nx,p%ny),hu_out(p%nx,p%ny),hv_out(p%nx,p%ny)
    real(dp), intent(out) :: n_out,min_depth,max_cfl
    logical, intent(out) :: ok
    real(dp) :: state(3,p%nx,p%ny),unext(3,p%nx,p%ny),time
    integer :: n
    logical :: guard_pass
    state(1,:,:)=h_in
    state(2,:,:)=hu_in
    state(3,:,:)=hv_in
    n_out=n_in
    min_depth=minval(h_in)
    max_cfl=p%dt*maxval((abs(hu_in/h_in)+sqrt(p%grav*h_in))/p%dx &
      +(abs(hv_in/h_in)+sqrt(p%grav*h_in))/p%dy)
    ok=.true.
    do n=0,p%planned-1
      time=p%start+n*p%dt
      call dynamics_shallow_water_time_update_2d_rk4__advance( &
        3,p%nx,p%ny,state,rhs,time,p%dt,unext,guard_pass)
      if (.not.guard_pass .or. minval(unext(1,:,:))<=0.0_dp) then
        ok=.false.
        error stop 'RK4 or wet-domain failure'
      end if
      state=unext
      n_out=n_out+1.0_dp
      min_depth=min(min_depth,minval(state(1,:,:)))
      max_cfl=max(max_cfl,p%dt*maxval( &
        (abs(state(2,:,:)/state(1,:,:))+sqrt(p%grav*state(1,:,:)))/p%dx &
        +(abs(state(3,:,:)/state(1,:,:))+sqrt(p%grav*state(1,:,:)))/p%dy))
    end do
    h_out=state(1,:,:)
    hu_out=state(2,:,:)
    hv_out=state(3,:,:)
  contains
    subroutine rhs(ncomp,nx,ny,t,U,dUdt)
      integer, intent(in) :: ncomp,nx,ny
      real(dp), intent(in) :: t,U(ncomp,nx,ny)
      real(dp), intent(out) :: dUdt(ncomp,nx,ny)
      real(dp) :: work(nx+4,ny+4),filled(nx+4,ny+4)
      real(dp) :: left(3,nx+1,ny),right(3,nx+1,ny)
      real(dp) :: bottom(3,nx,ny+1),top(3,nx,ny+1)
      real(dp) :: F(3,nx+1,ny),G(3,nx,ny+1),S(3,nx,ny),Q(3,nx,ny)
      real(dp) :: fx(3),gy(3),ax,ay
      integer :: c,i,j,wall_axis
      logical :: valid,odd,face_ok,all_ok
      if (minval(U(1,:,:))<=0.0_dp) error stop 'nonpositive stage depth'
      wall_axis=3-p%axis
      do c=1,3
        work=0.0_dp
        work(3:nx+2,3:ny+2)=U(c,:,:)
        odd=c==wall_axis+1
        call dynamics_shallow_water_boundary_2d_channel_mirror__apply( &
          nx,ny,2,nx+4,ny+4,wall_axis,odd,work,filled,valid)
        if (.not.valid) error stop 'boundary failure'
        call dynamics_shallow_water_reconstruction_2d_muscl_mc__reconstruct( &
          nx,ny,2,nx+4,ny+4,filled,left(c,:,:),right(c,:,:), &
          bottom(c,:,:),top(c,:,:),valid)
        if (.not.valid) error stop 'reconstruction failure'
      end do
      if (minval(left(1,:,:))<=0.0_dp .or. minval(right(1,:,:))<=0.0_dp &
        .or. minval(bottom(1,:,:))<=0.0_dp .or. minval(top(1,:,:))<=0.0_dp) &
        error stop 'nonpositive reconstructed depth'
      all_ok=.true.
      !$omp parallel do collapse(2) private(fx,gy,ax,ay,face_ok) &
      !$omp& reduction(.and.:all_ok) schedule(static)
      do j=1,ny
        do i=1,nx+1
          call dynamics_shallow_water_flux_2d_rusanov__compute_flux( &
            left(:,i,j),right(:,i,j),left(:,i,j),right(:,i,j), &
            p%grav,fx,gy,ax,ay,face_ok)
          F(:,i,j)=fx
          all_ok=all_ok.and.face_ok
        end do
      end do
      !$omp end parallel do
      !$omp parallel do collapse(2) private(fx,gy,ax,ay,face_ok) &
      !$omp& reduction(.and.:all_ok) schedule(static)
      do j=1,ny+1
        do i=1,nx
          call dynamics_shallow_water_flux_2d_rusanov__compute_flux( &
            bottom(:,i,j),top(:,i,j),bottom(:,i,j),top(:,i,j), &
            p%grav,fx,gy,ax,ay,face_ok)
          G(:,i,j)=gy
          all_ok=all_ok.and.face_ok
        end do
      end do
      !$omp end parallel do
      if (.not.all_ok) error stop 'flux failure'
      call dynamics_shallow_water_source_2d_coriolis__apply( &
        3,nx,ny,p%f,U,S,valid)
      if (.not.valid) error stop 'Coriolis failure'
      Q=0.0_dp
      if (p%profile==4) then
        call dynamics_shallow_water_source_2d_tc4_forcing__apply( &
          3,nx,ny,p%axis,p%x,p%y,t,p%f,p%dfdr,p%ubar,p%dubar,p%hbar, &
          p%grav,p%ls,p%psi0,p%rlow,p%ctr,p%sc0,p%rc,Q,valid)
        if (.not.valid) error stop 'forcing failure'
      end if
      !$omp parallel do collapse(2) schedule(static)
      do j=1,ny
        do i=1,nx
          dUdt(:,i,j)=-(F(:,i+1,j)-F(:,i,j))/p%dx &
            -(G(:,i,j+1)-G(:,i,j))/p%dy+S(:,i,j)+Q(:,i,j)
        end do
      end do
      !$omp end parallel do
    end subroutine rhs
  end subroutine shallow_water2d_channel__integrate
end module shallow_water2d_channel_model
