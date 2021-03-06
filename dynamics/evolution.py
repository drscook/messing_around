def run_experiment(part, walls, free_mem_to_use=0.5, write_to_file=True, report_period=None):
    start = timer()
    print("==================================================================================")
    
    with np.errstate(invalid='ignore'):
        initialize(part, walls)
        part.check()

    assert ((free_mem_to_use > 0.0) & (free_mem_to_use < 1.0)), f"free_mem_to_use must btw 0 & 1; given {free_mem_to_use}"

    part.write_to_file = write_to_file
    part.record_ptr = 0
    part.record_init(free_mem_to_use)
    part.record()
    
    if report_period is None:
        report_period = int(round(part.max_steps / 10))
    report_period = min(report_period, max_steps)
        
    print(f"Init complete.  Starting dynamics.")
    for step in range(1, part.max_steps+1):
#         print(f"Step {step}")
        next_state(part, walls)
#         part.check()
        
        if part.mode == 'parallel':
            update_gpu(part)
        
        part.record()

        if (step % report_period == 0) or (part.terminate):
            completed = step / part.max_steps
            elapsed = timer() - start
            predicted = elapsed / completed
            
            print(f"{part.mode} {part.num} particles, {completed*100:.0f}% complete, {time_format(elapsed)} elapsed, {time_format(predicted)} predicted")

    if part.write_to_file:
        part.data_file.close()

    

def get_pw_dt_cpu(part, walls):
    part.pw_dt_cpu = np.array([solver(w.get_pw_col_coefs(), part.pw_events_old[:, w.idx])[0] for w in walls]).T
    part.pw_dt = np.min(part.pw_dt_cpu)
    part.pw_events_new = (part.pw_dt_cpu < part.pw_dt + thresh)


def get_pp_dt_cpu(part, walls):
    part.pp_dt_cpu = solver(part.get_pp_col_coefs(), part.pp_events_old)[0]
    part.pp_dt = np.min(part.pp_dt_cpu)
    part.pp_events_new = (part.pp_dt_cpu < part.pp_dt + thresh)
    

def initialize(part, walls):
    global get_dt
    if np.all([w.dim == part.dim for w in walls]) == False:
        raise Exception('Not all walls and part dimensions agree')
        
    if np.all((part.gamma >= 0) & (part.gamma <= np.sqrt(2/part.dim))) == False:
        raise Exception(f"illegal mass distribution parameter {gamma}")
        
    part.pw_gap_min = []
    for (i, w) in enumerate(walls):
        w.idx = i
        w.pw_gap_min = w.pw_gap_m * part.radius + w.pw_gap_b
        part.pw_gap_min.append(w.pw_gap_min)
        if isinstance(w.pw_collision_law, PW_IgnoreLaw):
            w.data[0,0] = -1
    part.pw_gap_min = np.asarray(part.pw_gap_min).T
    part.pp_gap_min = cross_subtract(part.radius, -part.radius)
    np.fill_diagonal(part.pp_gap_min, -1)
    
    part.mom_inert = part.mass * (part.gamma * part.radius)**2
    part.sigma_vel = np.sqrt(BOLTZ * part.temp / part.mass)
    part.sigma_spin = np.sqrt(BOLTZ * part.temp / part.mom_inert)

    part.pos_loc = part.pos.copy()
    for p in range(part.num):
        if np.any(np.isinf(part.pos[p])):
            part.rand_pos(p)
            
        if np.any(np.isinf(part.vel[p])):
            part.rand_vel(p)
            
        if np.any(np.isinf(part.spin[p])):
            part.spin[p,:,:] = 0.0
#             part.rand_spin(p)

    
    if same_initial_speeds:
        speed = np.linalg.norm(part.vel[0])
        part.vel = make_unit(part.vel) * speed

    
    part.get_mesh()
    for w in walls:
        w.get_mesh()
    part.KE_init = part.get_KE()
    
    part.pw_dt = np.inf
    part.pp_dt = np.inf
    part.pw_events_old = np.full([part.num, len(walls)], False, dtype=bool)
    part.pp_events_old = np.full([part.num, part.num],   False, dtype=bool)
    
    part.pw_events_new = part.pw_events_old.copy()
    part.pp_events_new = part.pp_events_old.copy()
    
    

    if part.mode == 'serial':
        def get_dt(part, walls):
            get_pw_dt_cpu(part, walls)
            if (part.num > 1) & (not isinstance(part.pp_collision_law, PP_IgnoreLaw)):
                get_pp_dt_cpu(part, walls)

    elif part.mode == 'parallel':
        define_solver_gpu()
        init_gpu(part, walls)
        
        def get_dt(part, walls):
            get_pw_dt_gpu(part, walls)
            if check_gpu_cpu:
                get_pw_dt_cpu(part, walls)
                pw_check = np.allclose(part.pw_dt_cpu, part.pw_dt_gpu)
                if not pw_check:
                    raise Exception(f"cpu and gpu do NOT agree on pw_dt")
#                 else:
#                     print(f"cpu and gpu DO agree on pw_dt")
                
            if (part.num > 1) & (not isinstance(part.pp_collision_law, PP_IgnoreLaw)):
                get_pp_dt_gpu(part, walls)
                if check_gpu_cpu:
                    get_pp_dt_cpu(part, walls)
                    pp_check = np.allclose(part.pp_dt_cpu, part.pp_dt_gpu)
                    if not pp_check:
                        raise Exception(f"cpu and gpu do NOT agree on pp_dt")
#                     else:
#                         print(f"cpu and gpu DO agree on pp_dt")
    else:
        raise Exception(f"illegal mode {part.mode}")   
    

def find_cmplx(part):
    pw_counts = np.sum(part.pw_events_new, axis=-1)  # count p-w events for this particle
    pp_counts = np.sum(part.pp_events_new, axis=-1)  # count p-p events for this particle
    cmplx = np.nonzero((pw_counts + pp_counts) > 1)[0]  # finds particles with more than 1 event
    if len(cmplx) > 0:  # must handle complex events first
        part.record()  # record state before and after re-randomizing position to animations look right
        for p in cmplx:
            # re-randomize the particle's position            
            part.rand_pos(p)
            
            # clear its events
            part.pw_events_new[p,:] = False
            part.pp_events_new[p,:] = False
            part.pp_events_new[:,p] = False
    return cmplx

    
def next_state(part, walls, force=0):
    get_dt(part, walls)
    part.dt = min(part.pw_dt, part.pp_dt)
    
    if np.isinf(part.dt):
        raise Exception("No future collisions detected")
       
    if part.force is None:
        part.pos += part.vel * part.dt
        part.pos_loc += part.vel * part.dt
    else:  # Currently, force only works for cylinders and must be axial.  We plan to generalize this in the future.
        accel = part.force / part.mass
        part.pos[:,-1] += accel * part.dt**2 / 2 + part.vel[:,-1] * part.dt
        part.pos_loc[:,-1] += accel * part.dt**2 / 2 + part.vel[:,-1] * part.dt
        
        part.pos[:,:-1] += part.vel[:,:-1] * part.dt
        part.pos_loc[:,:-1] += part.vel[:,:-1] * part.dt
        part.vel[:,-1] += accel * part.dt

    part.t += part.dt
    
    if part.pw_dt >= part.dt + thresh:  # next event is p-p, so clear pw_events_new
        part.pw_events_new[:] = False

    if part.pp_dt >= part.dt + thresh:  # next event is p-w, so clear pp_events_new
        part.pp_events_new[:] = False
    
    cmplx = find_cmplx(part)
    if len(cmplx) > 0:
        print(f"COMPLEX COLLISION DETECTED. Re-randomized positions of particles {cmplx}")
        cmplx = find_cmplx(part)
        if len(cmplx) > 0:
            raise Exception(f"There are still complex collisions.  I don't understand why.  These particles are involved: {cmplx}")

    part.col = []
    for p, w in np.array(np.nonzero(part.pw_events_new)).T:  # resolve all p-w collisions
        part.col.append({'p':p, 'w':w})
        walls[w].resolve_pw_collision(part, walls, p)

    for p, q in np.array(np.nonzero(part.pp_events_new)).T:  # resolve all p-p collisions
        if p < q:
            part.col.append({'p':p, 'q':q})
            part.resolve_pp_collision(p, q)
            
    part.pw_events_old[:] = part.pw_events_new[:]
    part.pp_events_old[:] = part.pp_events_new[:]