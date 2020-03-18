import numpy as np
import sys, time, math, timeit
import threading

# OpenCV & Aruco
import cv2 as cv2
import cv2.aruco as aruco

# Crazyflie
import cflib.crtp # cf real-time protocol
from cflib.crazyflie import Crazyflie 
from cflib.positioning.motion_commander import MotionCommander
from cflib.crazyflie.syncCrazyflie import SyncCrazyflie

# Project specific imports
import uglyConst
from ugly_syncLog import UglyLogger

# Logging
import datetime

class controlMessage:
    def __init__(self):
        self.errorx = 0.0
        self.errory = 0.0
        self.errorz = 0.0
        self.erroryaw = 0.0
        self.cmd_zvel = 0.0

class stateMessage:
    def __init__(self):
        self.isMarkerDetected = False

#-- 180 deg rotation matrix around the x axis
R_flipx  = np.zeros((3,3), dtype=np.float32)
R_flipx[0,0] = 1.0
R_flipx[1,1] =-1.0
R_flipx[2,2] =-1.0

#-- 90 deg rotation matrix around z axis
R_flipz = np.zeros((3,3), dtype=np.float32)
#

#-- Translation relating large marker to small (8.935 cm in marker frame y direction)
T_slide = np.zeros((3,1),dtype=np.float32)
T_slide[1] = uglyConst.MARKER_OFFSET

# Checks if a matrix is a valid rotation matrix.
def isRotationMatrix(R):
    Rt = np.transpose(R)
    shouldBeIdentity = np.dot(Rt, R)
    I = np.identity(3, dtype=R.dtype)
    n = np.linalg.norm(I - shouldBeIdentity)
    return n < 1e-6

# Calculates rotation matrix to euler angles
# The result is the same as MATLAB except the order
# of the euler angles ( x and z are swapped ).
def rotationMatrixToEulerAngles(R):
    assert (isRotationMatrix(R))

    sy = math.sqrt(R[0, 0] * R[0, 0] + R[1, 0] * R[1, 0])

    singular = sy < 1e-6

    if not singular:
        x = math.atan2(R[2, 1], R[2, 2])
        y = math.atan2(-R[2, 0], sy)
        z = math.atan2(R[1, 0], R[0, 0])
    else:
        x = math.atan2(-R[1, 2], R[1, 1])
        y = math.atan2(-R[2, 0], sy)
        z = 0

    return np.array([x, y, z])


def init_cf():
    #--- Scan for cf
    cflib.crtp.init_drivers(enable_debug_driver=False)
    print('Scanning interfaces for Crazyflies...')
    available = cflib.crtp.scan_interfaces()

    if len(available) == 0:
        print("No cf found, aborting cf code.")
        return None, None
    else: 
        print('Crazyflies found:')
        for i in available:
            print(str(i[0]))
        URI = 'radio://0/80/2M' #str(available[0])
        cf = Crazyflie(rw_cache='./cache')
    
        return cf, URI


class ComputerVisionThread(threading.Thread):
    def __init__(self,ctrl_message, state_message, message_lock):
        """ constructor, setting initial variables """
        super(ComputerVisionThread,self).__init__()
        self.ctrl = ctrl_message
        self.state = state_message
        self.lock = message_lock

    def init_cv(self):
        cap = cv2.VideoCapture(uglyConst.CAM_NR)
        res = (cap.get(cv2.CAP_PROP_FRAME_WIDTH), cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        return cap, res

    def drawHUD(self, frame, resolution, yaw_camera):
        midpointx = int(resolution[0]/2)
        midpointy = int(resolution[1]/2)

        #-- Error crosshair
        cv2.drawMarker(frame, (midpointx-self.ctrl.errory*200,midpointy-self.ctrl.errorx*200),uglyConst.RED, markerType=cv2.MARKER_CROSS, thickness=2)

        #-- Anglometer
        cv2.ellipse(frame, (midpointx,midpointy),(10,10), -90, 0, -math.degrees(yaw_camera), uglyConst.BLACK, thickness=3)

    def loadCameraParams(self, cam_name):
        if cam_name is 'runcam_nano3':
            camera_matrix = np.array([[269.11459175467655, 0.0, 318.8896785174727], [0.0, 262.62554966204, 248.54894259248005], [0.0, 0.0, 1.0]])
            camera_dist = np.array([[-0.1913802179616581, 0.04076781232772304, -0.0014647104190866982, 0.00047321030718756505, -0.0043907605166862065]])
            calibration_error = 0.4467944666063116
        elif cam_name is 'runcam_nanolth':
            camera_matrix = np.array([[333.1833852111547, 0.0, 327.7723204462851], [0.0, 337.3376244908818, 229.96013817983925], [0.0, 0.0, 1.0]])
            camera_dist = np.array([[-0.37915663345130246, 0.18478180306843126, -0.00021990379249122642, -0.0014864903771132248, -0.05061040147030076]])
            calibration_error = 0.5077483684005625
        elif cam_name is 'webcam':
            camera_matrix = np.array([[1000.0, 0.0, 655], [0.0, 1000.0, 380], [0.0, 0.0, 1.0]])
            camera_dist = np.array([[-0.2, -1.3, -.0042, -.0025, 2.3]])
            calibration_error = 100
        else:
            print('Camera not found. Returning shit values.')
            camera_matrix = np.array([[0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]])
            camera_dist = np.array([[0.0, 0.0, 0.0, 0.0, 0.0]])
            calibration_error = 100

        return camera_matrix, camera_dist, calibration_error

    def save_frame(self, frame, name):
        now = datetime.datetime.now()
        date = now.strftime("%Y-%m-%d_%H%M")
        cv2.imwrite('figures/endFrame_'+name+'_'+date+'.jpg',frame)

    def undist_frame(self, frame, camera_matrix, camera_dist, resolution):
        new_camera_matrix, valid_pix_roi = cv2.getOptimalNewCameraMatrix(camera_matrix, camera_dist, (int(resolution[0]), int(resolution[1])), 0)
        mapx, mapy = cv2.initUndistortRectifyMap(camera_matrix, camera_dist, None, new_camera_matrix, (int(resolution[0]), int(resolution[1])), 5)
        if mapx is not None and mapy is not None:
            frame = cv2.remap(frame, mapx, mapy, cv2.INTER_LINEAR)
            return True, frame
        else: 
            return False, None

    def run(self):
        aruco_dict  = aruco.getPredefinedDictionary(aruco.DICT_4X4_50)
        parameters  = aruco.DetectorParameters_create()
        parameters.errorCorrectionRate = 1
        font = cv2.FONT_HERSHEY_PLAIN
        id2find = [10, 17] 
        marker_size  = [uglyConst.MARKERSIZE_BIG, uglyConst.MARKERSIZE_SMALL]

        camera_matrix, camera_distortion, _ = self.loadCameraParams('runcam_nano3')
        
        cap, resolution = self.init_cv()
        ids_seen = [0, 0]
        id2follow = 0
        sumTime = 0.0
        frameCount = 0
        
        while True:
            t0 = time.time() # start time for fps calculation
            ret, frame = cap.read()
            
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

            corners, ids, rejected = aruco.detectMarkers(image=gray, dictionary=aruco_dict, parameters=parameters, cameraMatrix=camera_matrix, distCoeff=camera_distortion)
            
            if ids is not None:
                self.state.isMarkerDetected = True

                #-- Draw the detected marker and put a reference frame over it
                aruco.drawDetectedMarkers(frame, corners)
                
                #-- Calculate which marker has been seen most at late
                if id2find[0] in ids:
                    ids_seen[0] += 1
                else: 
                    ids_seen[0] = 0
                
                if id2find[1] in ids:
                    ids_seen[1] += 2
                else: 
                    ids_seen[1] = 0
                
                id2follow = np.argmax(ids_seen) 
                idx_r, idx_c = np.where(ids == id2find[id2follow])
                
                #-- Extract the id to follow 
                corners = np.asarray(corners)
                corners = corners[idx_r, :]
               
                #-- Estimate poses of detected markers
                rvecs, tvecs, _ = aruco.estimatePoseSingleMarkers(corners, marker_size[id2follow], camera_matrix, camera_distortion)
                
                if rvecs is not None:
                    #-- Unpack the output, get only the first
                    rvec, tvec = rvecs[0,0,:], tvecs[0,0,:]     

                    #-- Draw the detected markers axis
                    for i in range(len(rvecs)):
                        aruco.drawAxis(frame, camera_matrix, camera_distortion, rvecs[i,0,:], tvecs[i,0,:], marker_size[id2follow]/2)
                    
                    #-- Obtain the rotation matrix tag->camera
                    R_ct = np.matrix(cv2.Rodrigues(rvec)[0])
                    R_tc = R_ct.T

                    #-- Now get Position and attitude of the camera respect to the marker
                    if id2follow == 0:
                        pos_camera = -R_tc*(np.matrix(tvec).T-T_slide)
                    else:
                        pos_camera = -R_tc*(np.matrix(tvec).T)

                    #-- Get the attitude of the camera respect to the frame
                    roll_camera, pitch_camera, yaw_camera = rotationMatrixToEulerAngles(R_flipx*R_tc)
                    pos_flip = np.array([[-pos_camera.item(1)], [pos_camera.item(0)]])
                    cmd_flip = np.array([[np.cos(yaw_camera), -np.sin(yaw_camera)], [np.sin(yaw_camera), np.cos(yaw_camera)]])
                    pos_cmd = cmd_flip.dot(pos_flip)

                    self.lock.acquire()
                    self.ctrl.errorx = pos_cmd[0]
                    self.ctrl.errory = pos_cmd[1]
                    self.ctrl.errorz = pos_camera[2]
                    self.ctrl.erroryaw = math.degrees(yaw_camera)
                    self.lock.release()

                    #-- Draw some information on frame
                    str_position = "Pos: x=%4.4f  y=%4.4f  z=%4.4f"%(pos_camera[0], pos_camera[1], pos_camera[2])
                    cv2.putText(frame, str_position, (0, 40), font, 1, uglyConst.BLACK, 2, cv2.LINE_AA)
                    str_attitude = "Att: roll=%4.4f  pitch=%4.4f  yaw (z)=%4.4f"%(math.degrees(roll_camera),math.degrees(pitch_camera),math.degrees(yaw_camera))
                    cv2.putText(frame, str_attitude, (0, 60), font, 1, uglyConst.BLACK, 2, cv2.LINE_AA)
                    
                    self.drawHUD(frame, resolution, yaw_camera)
            
            else:
                self.lock.acquire()
                self.state.isMarkerDetected = False
                self.lock.release()
            
            #-- Calculate and draw FPS on frame
            t1 = time.time() # end time for fps calculation
            sumTime = sumTime+(t1-t0)
            frameCount += 1

            str_fps = "Cur FPS: %4.4f, Avg FPS: %4.4f" %(1.0/(t1-t0), (frameCount/sumTime))
            cv2.putText(frame, str_fps, (0,20), font, 1, uglyConst.BLACK, 2, cv2.LINE_AA)

            #-- Middle crosshair
            cv2.drawMarker(frame, (int(resolution[0]/2),int(resolution[1]/2)),(0,0,0), markerType=cv2.MARKER_CROSS, thickness=2)

            #-- Update frame
            cv2.imshow('frame', frame)

            #-- User input
            key = cv2.waitKey(1) & 0xFF
            if key == ord('q'):
                #-- End thread
                self.save_frame(frame, 'dist')
                ret, frame = self.undist_frame(frame, camera_matrix, camera_distortion, resolution)
                if ret:
                    self.save_frame(frame, 'rect')
                cap.release()
                cv2.destroyAllWindows()
                print("Pressed 'Q' to exit CV thread.")
 
class CrazyflieThread(threading.Thread):
    def __init__(self, ctrl_message, state_message, message_lock):
        threading.Thread.__init__(self)
        self.ctrl = ctrl_message
        self.state = state_message
        self.lock = message_lock
        self.cf = None
        self.mc = None
        self.scf = None
        self.runSM = True
        self.cmd_height_old = uglyConst.TAKEOFF_HEIGHT
        self.landingController = uglyConst.CTRL_NONE # see uglyConst for controller choice

    def raise_exception(self):
        self.runSM = False

    #-- FSM condition funcitons
    def isCloseXYP(self, r):
        x = self.ctrl.errorx
        y = self.ctrl.errory
        if (np.sqrt(x*x+y*y) > r) or (np.abs(self.ctrl.erroryaw) > uglyConst.FAR_ANGL):
            return False
        else:
            return True

    def isCloseCone(self):
        x = self.ctrl.errorx
        y = self.ctrl.errory
        r = (self.mc._thread.get_height()-uglyConst.DIST_IGE)*uglyConst.FAR_CONE
        if (np.sqrt(x*x+y*y) > r): # or (np.abs(self.ctrl.erroryaw) > uglyConst.FAR_ANGL):
            return False
        else:
            return True

    #-- FSM state functions
    def stateInit(self):
        #--- Scan for cf
        cflib.crtp.init_drivers(enable_debug_driver=False)
        print('Scanning interfaces for Crazyflies...')
        available = cflib.crtp.scan_interfaces()

        if len(available) == 0:
            print("No cf found, aborting cf code.")
            self.cf = None
        else: 
            print('Crazyflies found:')
            for i in available:
                print(str(i[0]))
            self.URI = 'radio://0/80/2M' #available[0][0]
            self.cf = Crazyflie(rw_cache='./cache')
        
        if self.cf is None:
            print('Not running cf code.')
            return None
        
        self.scf = SyncCrazyflie(self.URI,cf=Crazyflie(rw_cache='./cache'))
        self.scf.open_link()
        self.mc = MotionCommander(self.scf)
        self.file = open("ugly_log.txt","a")
        self.lgr = UglyLogger(self.URI, self.scf, self.file)

        self.enterTakingOff()
        return self.stateTakingOff

    def enterTakingOff(self):
        print("enterTakingOff")
        self.mc.take_off(uglyConst.TAKEOFF_HEIGHT,uglyConst.TAKEOFF_ZVEL)

    def stateTakingOff(self):
        print("stateTakingOff")
        
        if  self.mc._thread.get_height() >= uglyConst.TAKEOFF_HEIGHT:
            return self.stateSeeking
        else:
            time.sleep(0.05)
            return self.stateTakingOff

    def stateSeeking(self):
        print("stateSeeking")
        self.mc._set_vel_setpoint(0.0, 0.0, 0.01, 0.0)
        
        if self.state.isMarkerDetected:
            return self.stateNearing
        else:
            time.sleep(0.05)
            return self.stateSeeking

    def stateNearing(self):
        self.mc._set_vel_setpoint(self.ctrl.errorx*uglyConst.Kx, self.ctrl.errory*uglyConst.Ky, 0.0, 0.0)
        print("stateNearing")
        
        if not self.state.isMarkerDetected:
            return self.stateSeeking
        elif self.isCloseCone():
            return self.stateApproaching
        else:
            time.sleep(0.05)
            return self.stateNearing

    def stateApproaching(self):
        print("stateApproaching")
        self.mc._set_vel_setpoint(self.ctrl.errorx*uglyConst.Kx, self.ctrl.errory*uglyConst.Ky, -uglyConst.APPROACH_ZVEL, -self.ctrl.erroryaw*uglyConst.Kyaw)
        if not self.isCloseCone:
            return self.stateNearing
        if self.mc._thread.get_height() < (uglyConst.DIST_IGE - uglyConst.DIST_IGE_HYST):
            if self.landingController == uglyConst.CTRL_NONE:
                return self.stateLandning
            elif self.landingController == uglyConst.CTRL_POSD:
                self.enterLandingIGE()
                return self.stateLandingIGE
        else:
            time.sleep(0.01)
            return self.stateApproaching

    def stateLandning(self):
        print("stateLanding")
        self.mc._set_vel_setpoint(self.ctrl.errorx*uglyConst.Kx, self.ctrl.errory*uglyConst.Ky, -uglyConst.LANDING_ZVEL, -self.ctrl.erroryaw*uglyConst.Kyaw)

        if self.mc._thread.get_height() > (uglyConst.DIST_IGE + uglyConst.DIST_IGE_HYST):
            return self.stateApproaching
        elif self.mc._thread.get_height() < uglyConst.LANDING_HEIGHT:
            return self.exitLanding()
        else:
            time.sleep(0.05)
            return self.stateLandning

    def enterLandingIGE(self):
        print("enterLandingIGE")
        self.cmd_height_old = self.mc._thread.get_height()
        return self.stateLandingIGE

    def stateLandingIGE(self):
        print("stateLandingIGE")
        cmd_height = ref_height - self.mc._thread.get_height()

        D = cmd_height-self.cmd_height_old

        cmd_zvel = (D-self.ctrl.cmd_zvel) / 2.0 # smoothing, for fixed frame rate!!! :O

        self.mc._set_vel_setpoint(
            self.ctrl.errorx*uglyConst.Kx, 
            self.ctrl.errory*uglyConst.Ky, 
            cmd_zvel*uglyConst.Kz, 
            -self.ctrl.erroryaw*uglyConst.Kyaw)

        self.cmd_height_old = cmd_height

        if self.mc._thread.get_height() > (uglyConst.DIST_IGE + uglyConst.DIST_IGE_HYST):
            return self.stateApproaching
        elif self.mc._thread.get_height() < uglyConst.LANDING_HEIGHT:
            self.exitLanding()
            return self.stateLanded
        else:
            time.sleep(0.05)
            return self.stateLandingIGE

    def exitLanding(self):
        print("exitLandning")
        self.mc._set_vel_setpoint(self.ctrl.errorx*uglyConst.Kx, self.ctrl.errory*uglyConst.Ky, 0.0, -self.ctrl.erroryaw*uglyConst.Kyaw)
        
        if self.isCloseXYP(uglyConst.LADNING_DIST):
            self.mc.stop()
            self.mc.land()
            return self.stateLanded
        else:
            time.sleep(0.05)
            return self.exitLanding

    def stateLanded(self):
        print("stateLanded")
        return None

    def run(self):
        try: 
            state = self.stateInit    # initial state
            while state and self.runSM: 
                state = state()

        finally:
            print('Stopping cf_thread')
            if self.cf is not None:
                if self.mc._is_flying:
                    self.mc.stop()
                    print('Finally landing')
                    self.mc.land()
                self.scf.close_link()
                self.file.close()
                  
if __name__ == '__main__':
    #print('Python version: '+sys.version)
    #print('OpenCV version: '+cv2.__version__)

    state_message = stateMessage()
    ctrl_message = controlMessage()
    message_lock = threading.Lock()
    
    cv_thread = ComputerVisionThread(ctrl_message, state_message, message_lock)
    cf_thread = CrazyflieThread(ctrl_message, state_message, message_lock)

    #-- Starting threads
    cv_thread.start()
    cf_thread.start()

    #-- Stopping threads
    cv_thread.join()
    print('cv_thread stopped.')
    cf_thread.raise_exception()
    cf_thread.join()
    print('cf_thread stopped.')

        