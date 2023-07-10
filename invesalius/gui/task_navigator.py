#--------------------------------------------------------------------------
# Software:     InVesalius - Software de Reconstrucao 3D de Imagens Medicas
# Copyright:    (C) 2001  Centro de Pesquisas Renato Archer
# Homepage:     http://www.softwarepublico.gov.br
# Contact:      invesalius@cti.gov.br
# License:      GNU - GPL 2 (LICENSE.txt/LICENCA.txt)
#--------------------------------------------------------------------------
#    Este programa e software livre; voce pode redistribui-lo e/ou
#    modifica-lo sob os termos da Licenca Publica Geral GNU, conforme
#    publicada pela Free Software Foundation; de acordo com a versao 2
#    da Licenca.
#
#    Este programa eh distribuido na expectativa de ser util, mas SEM
#    QUALQUER GARANTIA; sem mesmo a garantia implicita de
#    COMERCIALIZACAO ou de ADEQUACAO A QUALQUER PROPOSITO EM
#    PARTICULAR. Consulte a Licenca Publica Geral GNU para obter mais
#    detalhes.
#--------------------------------------------------------------------------
import os

import dataclasses
from functools import partial
import itertools
import time

import nibabel as nb
import numpy as np
try:
    import Trekker
    has_trekker = True
except ImportError:
    has_trekker = False

try:
    #TODO: the try-except could be done inside the mTMS() method call
    from invesalius.navigation.mtms import mTMS
    mTMS()
    has_mTMS = True
except:
    has_mTMS = False

import wx

try:
    import wx.lib.agw.foldpanelbar as fpb
except ImportError:
    import wx.lib.foldpanelbar as fpb

import wx.lib.colourselect as csel
import wx.lib.masked.numctrl
from invesalius.pubsub import pub as Publisher

import invesalius.constants as const
import invesalius.data.brainmesh_handler as brain

import invesalius.data.imagedata_utils as imagedata_utils
import invesalius.data.slice_ as sl
import invesalius.data.tractography as dti
import invesalius.data.record_coords as rec
import invesalius.data.vtk_utils as vtk_utils
import invesalius.data.bases as db
import invesalius.data.coregistration as dcr
import invesalius.gui.dialogs as dlg
import invesalius.project as prj
import invesalius.session as ses

from invesalius import utils
from invesalius.gui import utils as gui_utils
from invesalius.navigation.iterativeclosestpoint import IterativeClosestPoint
from invesalius.navigation.navigation import Navigation
from invesalius.navigation.image import Image
from invesalius.navigation.tracker import Tracker

from invesalius.navigation.robot import Robot
from invesalius.data.converters import to_vtk, convert_custom_bin_to_vtk

from invesalius.net.neuronavigation_api import NeuronavigationApi

HAS_PEDAL_CONNECTION = True
try:
    from invesalius.net.pedal_connection import PedalConnection
except ImportError:
    HAS_PEDAL_CONNECTION = False

from invesalius import inv_paths

BTN_NEW = wx.NewId()
BTN_IMPORT_LOCAL = wx.NewId()


class TaskPanel(wx.Panel):
    def __init__(self, parent):
        wx.Panel.__init__(self, parent)

        inner_panel = InnerTaskPanel(self)

        sizer = wx.BoxSizer(wx.HORIZONTAL)
        sizer.Add(inner_panel, 1, wx.EXPAND|wx.GROW|wx.BOTTOM|wx.RIGHT |
                  wx.LEFT, 7)
        sizer.Fit(self)

        self.SetSizer(sizer)
        self.Update()
        self.SetAutoLayout(1)


class InnerTaskPanel(wx.Panel):
    def __init__(self, parent):
        wx.Panel.__init__(self, parent)
        default_colour = self.GetBackgroundColour()
        background_colour = wx.Colour(255,255,255)
        self.SetBackgroundColour(background_colour)

        txt_nav = wx.StaticText(self, -1, _('Select fiducials and navigate'),
                                size=wx.Size(90, 20))
        txt_nav.SetFont(wx.Font(9, wx.DEFAULT, wx.NORMAL, wx.BOLD))

        # Create horizontal sizer to represent lines in the panel
        txt_sizer = wx.BoxSizer(wx.HORIZONTAL)
        txt_sizer.Add(txt_nav, 1, wx.EXPAND|wx.GROW, 5)

        # Fold panel which contains navigation configurations
        fold_panel = FoldPanel(self)
        fold_panel.SetBackgroundColour(default_colour)

        # Add line sizer into main sizer
        main_sizer = wx.BoxSizer(wx.VERTICAL)
        main_sizer.Add(txt_sizer, 0, wx.GROW|wx.EXPAND|wx.LEFT|wx.RIGHT, 5)
        main_sizer.Add(fold_panel, 1, wx.GROW|wx.EXPAND|wx.LEFT|wx.RIGHT, 5)
        main_sizer.AddSpacer(5)
        main_sizer.Fit(self)

        self.SetSizerAndFit(main_sizer)
        self.Update()
        self.SetAutoLayout(1)

        self.sizer = main_sizer


class FoldPanel(wx.Panel):
    def __init__(self, parent):
        wx.Panel.__init__(self, parent)

        inner_panel = InnerFoldPanel(self)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(inner_panel, 0, wx.EXPAND|wx.GROW)
        sizer.Fit(self)

        self.SetSizerAndFit(sizer)
        self.Update()
        self.SetAutoLayout(1)


class InnerFoldPanel(wx.Panel):
    def __init__(self, parent):
        wx.Panel.__init__(self, parent)
        try:
            default_colour = wx.SystemSettings.GetColour(wx.SYS_COLOUR_MENUBAR)
        except AttributeError:
            default_colour = wx.SystemSettings_GetColour(wx.SYS_COLOUR_MENUBAR)
        self.SetBackgroundColour(default_colour)

        self.__bind_events()
        # Fold panel and its style settings
        # FIXME: If we dont insert a value in size or if we set wx.DefaultSize,
        # the fold_panel doesnt show. This means that, for some reason, Sizer
        # is not working properly in this panel. It might be on some child or
        # parent panel. Perhaps we need to insert the item into the sizer also...
        # Study this.

        fold_panel = fpb.FoldPanelBar(self, -1, wx.DefaultPosition,
                                      (10, 330), 0, fpb.FPB_SINGLE_FOLD)

        # Initialize Navigation, Tracker, Robot, Image, and PedalConnection objects here to make them
        # available to several panels.
        #
        tracker = Tracker()
        robot = Robot(
            tracker=tracker
        )
        image = Image()
        pedal_connection = PedalConnection() if HAS_PEDAL_CONNECTION else None
        icp = IterativeClosestPoint()
        neuronavigation_api = NeuronavigationApi()
        navigation = Navigation(
            pedal_connection=pedal_connection,
            neuronavigation_api=neuronavigation_api,
        )

        # TODO: Initialize checkboxes before panels: they are updated by ObjectRegistrationPanel when loading its state.
        #   A better solution would be to have these checkboxes save their own state, independent of the panels, but that's
        #   not implemented yet.

        # Checkbox for camera update in volume rendering during navigation
        tooltip = wx.ToolTip(_("Update camera in volume"))
        checkcamera = wx.CheckBox(self, -1, _('Vol. camera'))
        checkcamera.SetToolTip(tooltip)
        checkcamera.SetValue(const.CAM_MODE)
        checkcamera.Bind(wx.EVT_CHECKBOX, self.OnVolumeCameraCheckbox)
        self.checkcamera = checkcamera

        # Checkbox to use serial port to trigger pulse signal and create markers
        tooltip = wx.ToolTip(_("Enable serial port communication to trigger pulse and create markers"))
        checkbox_serial_port = wx.CheckBox(self, -1, _('Serial port'))
        checkbox_serial_port.SetToolTip(tooltip)
        checkbox_serial_port.SetValue(False)
        checkbox_serial_port.Bind(wx.EVT_CHECKBOX, partial(self.OnEnableSerialPort, ctrl=checkbox_serial_port))
        self.checkbox_serial_port = checkbox_serial_port

        # Checkbox for object position and orientation update in volume rendering during navigation
        tooltip = wx.ToolTip(_("Show and track TMS coil"))
        checkobj = wx.CheckBox(self, -1, _('Show coil'))
        checkobj.SetToolTip(tooltip)
        checkobj.SetValue(False)
        checkobj.Disable()
        checkobj.Bind(wx.EVT_CHECKBOX, self.OnShowCoil)
        self.checkobj = checkobj

        #  if sys.platform != 'win32':
        self.checkcamera.SetWindowVariant(wx.WINDOW_VARIANT_SMALL)
        checkbox_serial_port.SetWindowVariant(wx.WINDOW_VARIANT_SMALL)
        checkobj.SetWindowVariant(wx.WINDOW_VARIANT_SMALL)

        # Fold panel style
        style = fpb.CaptionBarStyle()
        style.SetCaptionStyle(fpb.CAPTIONBAR_GRADIENT_V)
        style.SetFirstColour(default_colour)
        style.SetSecondColour(default_colour)

        # Fold 1 - Navigation panel
        item = fold_panel.AddFoldPanel(_("Neuronavigation"), collapsed=True)
        ntw = NeuronavigationPanel(
            parent=item,
            navigation=navigation,
            tracker=tracker,
            robot=robot,
            icp=icp,
            image=image,
            pedal_connection=pedal_connection,
            neuronavigation_api=neuronavigation_api,
        )

        fold_panel.ApplyCaptionStyle(item, style)
        fold_panel.AddFoldPanelWindow(item, ntw, spacing=0,
                                      leftSpacing=0, rightSpacing=0)
        fold_panel.Expand(fold_panel.GetFoldPanel(0))

        # Fold 2 - Object registration panel
        item = fold_panel.AddFoldPanel(_("Object registration"), collapsed=True)
        otw = ObjectRegistrationPanel(
            parent=item,
            tracker=tracker,
            pedal_connection=pedal_connection,
            neuronavigation_api=neuronavigation_api,
        )

        fold_panel.ApplyCaptionStyle(item, style)
        fold_panel.AddFoldPanelWindow(item, otw, spacing=0,
                                      leftSpacing=0, rightSpacing=0)

        # Fold 3 - Markers panel
        item = fold_panel.AddFoldPanel(_("Markers"), collapsed=True)
        mtw = MarkersPanel(item, navigation, tracker, icp)

        fold_panel.ApplyCaptionStyle(item, style)
        fold_panel.AddFoldPanelWindow(item, mtw, spacing= 0,
                                      leftSpacing=0, rightSpacing=0)

        # Fold 4 - Tractography panel
        if has_trekker:
            item = fold_panel.AddFoldPanel(_("Tractography"), collapsed=True)
            otw = TractographyPanel(item)

            fold_panel.ApplyCaptionStyle(item, style)
            fold_panel.AddFoldPanelWindow(item, otw, spacing=0,
                                          leftSpacing=0, rightSpacing=0)

        # Fold 5 - DBS
        self.dbs_item = fold_panel.AddFoldPanel(_("Deep Brain Stimulation"), collapsed=True)
        dtw = DbsPanel(self.dbs_item) #Atribuir nova var, criar panel

        fold_panel.ApplyCaptionStyle(self.dbs_item, style)
        fold_panel.AddFoldPanelWindow(self.dbs_item, dtw, spacing= 0,
                                      leftSpacing=0, rightSpacing=0)
        self.dbs_item.Hide()

        # Fold 6 - Sessions
        item = fold_panel.AddFoldPanel(_("Sessions"), collapsed=False)
        stw = SessionPanel(item)
        fold_panel.ApplyCaptionStyle(item, style)
        fold_panel.AddFoldPanelWindow(item, stw, spacing= 0,
                                      leftSpacing=0, rightSpacing=0)

        # Fold 7 - E-field

        item = fold_panel.AddFoldPanel(_("E-field"), collapsed=True)
        etw = E_fieldPanel(item, navigation)
        fold_panel.ApplyCaptionStyle(item, style)
        fold_panel.AddFoldPanelWindow(item, etw, spacing=0,
                                        leftSpacing=0, rightSpacing=0)

        # Panel sizer for checkboxes
        line_sizer = wx.BoxSizer(wx.HORIZONTAL)
        line_sizer.Add(checkcamera, 0, wx.ALIGN_LEFT | wx.RIGHT | wx.LEFT, 5)
        line_sizer.Add(checkbox_serial_port, 0, wx.ALIGN_CENTER)
        line_sizer.Add(checkobj, 0, wx.RIGHT | wx.LEFT, 5)
        line_sizer.Fit(self)

        # Panel sizer to expand fold panel
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(fold_panel, 0, wx.GROW|wx.EXPAND)
        sizer.Add(line_sizer, 1, wx.GROW | wx.EXPAND)
        sizer.Fit(self)

        self.track_obj = False

        self.SetSizer(sizer)
        self.Update()
        self.SetAutoLayout(1)
        
    def __bind_events(self):
        Publisher.subscribe(self.OnCheckStatus, 'Navigation status')
        Publisher.subscribe(self.OnShowDbs, "Show dbs folder")
        Publisher.subscribe(self.OnHideDbs, "Hide dbs folder")

        # Externally check/uncheck and enable/disable checkboxes.
        Publisher.subscribe(self.CheckShowCoil, 'Check show-coil checkbox')
        Publisher.subscribe(self.CheckVolumeCameraCheckbox, 'Check volume camera checkbox')

        Publisher.subscribe(self.EnableShowCoil, 'Enable show-coil checkbox')
        Publisher.subscribe(self.EnableVolumeCameraCheckbox, 'Enable volume camera checkbox')

    def OnShowDbs(self):
        self.dbs_item.Show()

    def OnHideDbs(self):
        self.dbs_item.Hide()

    def OnCheckStatus(self, nav_status, vis_status):
        if nav_status:
            self.checkbox_serial_port.Enable(False)
            self.checkobj.Enable(False)
        else:
            self.checkbox_serial_port.Enable(True)
            if self.track_obj:
                self.checkobj.Enable(True)

    def OnEnableSerialPort(self, evt, ctrl):
        if ctrl.GetValue():
            from wx import ID_OK
            dlg_port = dlg.SetCOMPort(select_baud_rate=False)

            if dlg_port.ShowModal() != ID_OK:
                ctrl.SetValue(False)
                return

            com_port = dlg_port.GetCOMPort()
            baud_rate = 115200

            Publisher.sendMessage('Update serial port', serial_port_in_use=True, com_port=com_port, baud_rate=baud_rate)
        else:
            Publisher.sendMessage('Update serial port', serial_port_in_use=False)

    # 'Show coil' checkbox

    def CheckShowCoil(self, checked=False):
        self.checkobj.SetValue(checked)
        self.track_obj = checked

        self.OnShowCoil()

    def EnableShowCoil(self, enabled=False):
        self.checkobj.Enable(enabled)

    def OnShowCoil(self, evt=None):
        checked = self.checkobj.GetValue()
        Publisher.sendMessage('Show-coil checked', checked=checked)

    # 'Volume camera' checkbox

    def CheckVolumeCameraCheckbox(self, checked):
        self.checkcamera.SetValue(checked)
        self.OnVolumeCameraCheckbox()

    def OnVolumeCameraCheckbox(self, evt=None, status=None):
        Publisher.sendMessage('Update volume camera state', camera_state=self.checkcamera.GetValue())

    def EnableVolumeCameraCheckbox(self, enabled):
        self.checkcamera.Enable(enabled)

class NeuronavigationPanel(wx.Panel):
    def __init__(self, parent, navigation, tracker, robot, icp, image, pedal_connection, neuronavigation_api):
        wx.Panel.__init__(self, parent)
        try:
            default_colour = wx.SystemSettings.GetColour(wx.SYS_COLOUR_MENUBAR)
        except AttributeError:
            default_colour = wx.SystemSettings_GetColour(wx.SYS_COLOUR_MENUBAR)
        self.SetBackgroundColour(default_colour)

        self.SetAutoLayout(1)

        self.__bind_events()

        # Initialize global variables
        self.pedal_connection = pedal_connection
        self.neuronavigation_api = neuronavigation_api

        self.navigation = navigation
        self.icp = icp
        self.tracker = tracker
        self.robot = robot
        self.image = image

        self.nav_status = False
        self.tracker_fiducial_being_set = None
        self.current_coord = 0, 0, 0, None, None, None

        # Initialize list of buttons and numctrls for wx objects
        self.btns_set_fiducial = [None, None, None, None, None, None]
        self.numctrls_fiducial = [[], [], [], [], [], []]

        # ComboBox for spatial tracker device selection
        tracker_options = [_("Select tracker:")] + self.tracker.get_trackers()
        select_tracker_elem = wx.ComboBox(self, -1, "", size=(145, -1),
                                          choices=tracker_options, style=wx.CB_DROPDOWN|wx.CB_READONLY)

        tooltip = wx.ToolTip(_("Choose the tracking device"))
        select_tracker_elem.SetToolTip(tooltip)

        select_tracker_elem.SetSelection(self.tracker.tracker_id)
        select_tracker_elem.Bind(wx.EVT_COMBOBOX, partial(self.OnChooseTracker, ctrl=select_tracker_elem))
        self.select_tracker_elem = select_tracker_elem

        # ComboBox for tracker reference mode
        tooltip = wx.ToolTip(_("Choose the navigation reference mode"))
        choice_ref = wx.ComboBox(self, -1, "",
                                 choices=const.REF_MODE, style=wx.CB_DROPDOWN|wx.CB_READONLY)
        choice_ref.SetSelection(const.DEFAULT_REF_MODE)
        choice_ref.SetToolTip(tooltip)
        choice_ref.Bind(wx.EVT_COMBOBOX, partial(self.OnChooseReferenceMode, ctrl=select_tracker_elem))
        self.choice_ref = choice_ref

        # Toggle buttons for image fiducials
        for n, fiducial in enumerate(const.IMAGE_FIDUCIALS):
            button_id = fiducial['button_id']
            label = fiducial['label']
            tip = fiducial['tip']

            ctrl = wx.ToggleButton(self, button_id, label=label)
            ctrl.SetMinSize((gui_utils.calc_width_needed(ctrl, 3), -1))
            ctrl.SetToolTip(wx.ToolTip(tip))
            ctrl.Bind(wx.EVT_TOGGLEBUTTON, partial(self.OnImageFiducials, n))
            ctrl.SetValue(self.image.IsImageFiducialSet(n))

            self.btns_set_fiducial[n] = ctrl

        # Push buttons for tracker fiducials
        for n, fiducial in enumerate(const.TRACKER_FIDUCIALS):
            button_id = fiducial['button_id']
            label = fiducial['label']
            tip = fiducial['tip']

            ctrl = wx.ToggleButton(self, button_id, label=label)
            ctrl.SetMinSize((gui_utils.calc_width_needed(ctrl, 3), -1))
            ctrl.SetToolTip(wx.ToolTip(tip))
            ctrl.Bind(wx.EVT_TOGGLEBUTTON, partial(self.OnTrackerFiducials, n, ctrl=ctrl))

            self.btns_set_fiducial[n + 3] = ctrl

        # TODO: Find a better alignment between FRE, text and navigate button

        # Fiducial registration error text and checkbox
        txt_fre = wx.StaticText(self, -1, _('FRE:'))
        tooltip = wx.ToolTip(_("Fiducial registration error"))

        # XXX: Currently always shows ICP corrected FRE (fiducial registration error) initially
        #   in the FRE textbox. This is a compromise, done due to corrected and non-corrected FRE values
        #   being split between Navigation and IterativeClosestPoint classes, and hence it being
        #   difficult to access both at this stage. This could be improved, e.g., by creating
        #   a separate class, which would hold both FRE values and would also know whether ICP
        #   corrected or non-corrected value is being used.
        #
        value = self.icp.GetFreForUI()

        txtctrl_fre = wx.TextCtrl(self, value=value, size=wx.Size(60, -1), style=wx.TE_CENTRE)
        txtctrl_fre.SetFont(wx.Font(9, wx.DEFAULT, wx.NORMAL, wx.BOLD))
        txtctrl_fre.SetBackgroundColour('WHITE')
        txtctrl_fre.SetEditable(0)
        txtctrl_fre.SetToolTip(tooltip)
        self.txtctrl_fre = txtctrl_fre

        # Toggle button for neuronavigation
        tooltip = wx.ToolTip(_("Start navigation"))
        btn_nav = wx.ToggleButton(self, -1, _("Navigate"), size=wx.Size(80, -1))
        btn_nav.SetToolTip(tooltip)
        btn_nav.Bind(wx.EVT_TOGGLEBUTTON, partial(self.OnNavigate, btn_nav=btn_nav))

        # "Refine" text and checkbox
        txt_icp = wx.StaticText(self, -1, _('Refine:'))
        tooltip = wx.ToolTip(_(u"Refine the coregistration"))
        checkbox_icp = wx.CheckBox(self, -1, _(' '))
        checkbox_icp.SetValue(self.icp.use_icp)
        checkbox_icp.Enable(False)
        checkbox_icp.Bind(wx.EVT_CHECKBOX, partial(self.OnCheckboxICP, ctrl=checkbox_icp))
        checkbox_icp.SetToolTip(tooltip)
        self.checkbox_icp = checkbox_icp

        # "Pedal pressed" text and an indicator (checkbox) for pedal press
        if (pedal_connection is not None and pedal_connection.in_use) or neuronavigation_api is not None:
            txt_pedal_pressed = wx.StaticText(self, -1, _('Pedal pressed:'))
            tooltip = wx.ToolTip(_(u"Is the pedal pressed"))
            checkbox_pedal_pressed = wx.CheckBox(self, -1, _(' '))
            checkbox_pedal_pressed.SetValue(False)
            checkbox_pedal_pressed.Enable(False)
            checkbox_pedal_pressed.SetToolTip(tooltip)

            if pedal_connection is not None:
                pedal_connection.add_callback(name='gui', callback=checkbox_pedal_pressed.SetValue)

            if neuronavigation_api is not None:
                neuronavigation_api.add_pedal_callback(name='gui', callback=checkbox_pedal_pressed.SetValue)

            self.checkbox_pedal_pressed = checkbox_pedal_pressed
        else:
            txt_pedal_pressed = None
            self.checkbox_pedal_pressed = None

        # "Lock to target" text and checkbox
        tooltip = wx.ToolTip(_(u"Allow triggering stimulation pulse only if the coil is at the target"))
        lock_to_target_text = wx.StaticText(self, -1, _('Lock to target:'))
        lock_to_target_checkbox = wx.CheckBox(self, -1, _(' '))
        lock_to_target_checkbox.SetValue(False)
        lock_to_target_checkbox.Enable(False)
        lock_to_target_checkbox.Bind(wx.EVT_CHECKBOX, partial(self.OnLockToTargetCheckbox, ctrl=lock_to_target_checkbox))
        lock_to_target_checkbox.SetToolTip(tooltip)

        self.lock_to_target_checkbox = lock_to_target_checkbox

        # Image and tracker coordinates number controls
        for m in range(len(self.btns_set_fiducial)):
            for n in range(3):
                if m <= 2:
                    value = self.image.GetImageFiducialForUI(m, n)
                else:
                    value = self.tracker.GetTrackerFiducialForUI(m - 3, n)

                self.numctrls_fiducial[m].append(
                    wx.lib.masked.numctrl.NumCtrl(parent=self, integerWidth=4, fractionWidth=1, value=value))

        # Sizers to group all GUI objects
        choice_sizer = wx.FlexGridSizer(rows=1, cols=2, hgap=5, vgap=5)
        choice_sizer.AddMany([(select_tracker_elem, wx.LEFT),
                              (choice_ref, wx.RIGHT)])

        coord_sizer = wx.GridBagSizer(hgap=5, vgap=5)

        for m in range(len(self.btns_set_fiducial)):
            coord_sizer.Add(self.btns_set_fiducial[m], pos=wx.GBPosition(m, 0))
            for n in range(3):
                coord_sizer.Add(self.numctrls_fiducial[m][n], pos=wx.GBPosition(m, n+1))
                if m in range(1, 6):
                    self.numctrls_fiducial[m][n].SetEditable(False)

        nav_sizer = wx.FlexGridSizer(rows=1, cols=5, hgap=5, vgap=5)
        nav_sizer.AddMany([(txt_fre, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.ALIGN_CENTER_VERTICAL),
                           (txtctrl_fre, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.ALIGN_CENTER_VERTICAL),
                           (btn_nav, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.ALIGN_CENTER_VERTICAL),
                           (txt_icp, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.ALIGN_CENTER_VERTICAL),
                           (checkbox_icp, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.ALIGN_CENTER_VERTICAL)])

        checkboxes_sizer = wx.FlexGridSizer(rows=1, cols=4, hgap=5, vgap=5)
        checkboxes_sizer.AddMany([(lock_to_target_text, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.ALIGN_CENTER_VERTICAL),
                                  (lock_to_target_checkbox, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.ALIGN_CENTER_VERTICAL)])

        if (pedal_connection is not None and pedal_connection.in_use) or neuronavigation_api is not None:
            checkboxes_sizer.AddMany([(txt_pedal_pressed, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.ALIGN_CENTER_VERTICAL),
                                      (checkbox_pedal_pressed, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.ALIGN_CENTER_VERTICAL)])

        group_sizer = wx.FlexGridSizer(rows=10, cols=1, hgap=5, vgap=5)
        group_sizer.AddGrowableCol(0, 1)
        group_sizer.AddGrowableRow(0, 1)
        group_sizer.AddGrowableRow(1, 1)
        group_sizer.AddGrowableRow(2, 1)
        group_sizer.SetFlexibleDirection(wx.BOTH)
        group_sizer.AddMany([(choice_sizer, 0, wx.ALIGN_CENTER_HORIZONTAL),
                             (coord_sizer, 0, wx.ALIGN_CENTER_HORIZONTAL),
                             (nav_sizer, 0, wx.ALIGN_CENTER_HORIZONTAL),
                             (checkboxes_sizer, 0, wx.ALIGN_CENTER_HORIZONTAL)])

        main_sizer = wx.BoxSizer(wx.HORIZONTAL)
        main_sizer.Add(group_sizer, 1)# wx.ALIGN_CENTER_HORIZONTAL, 10)
        self.sizer = main_sizer
        self.SetSizer(main_sizer)
        self.Fit()

    def __bind_events(self):
        Publisher.subscribe(self.LoadImageFiducials, 'Load image fiducials')
        Publisher.subscribe(self.SetImageFiducial, 'Set image fiducial')
        Publisher.subscribe(self.SetTrackerFiducial, 'Set tracker fiducial')
        Publisher.subscribe(self.UpdateImageCoordinates, 'Set cross focal point')
        Publisher.subscribe(self.DisconnectTracker, 'Disconnect tracker')
        Publisher.subscribe(self.OnCloseProject, 'Close project data')
        Publisher.subscribe(self.UpdateTrekkerObject, 'Update Trekker object')
        Publisher.subscribe(self.UpdateNumTracts, 'Update number of tracts')
        Publisher.subscribe(self.UpdateSeedOffset, 'Update seed offset')
        Publisher.subscribe(self.UpdateSeedRadius, 'Update seed radius')
        Publisher.subscribe(self.UpdateSleep, 'Update sleep')
        Publisher.subscribe(self.UpdateNumberThreads, 'Update number of threads')
        Publisher.subscribe(self.UpdateTractsVisualization, 'Update tracts visualization')
        Publisher.subscribe(self.UpdatePeelVisualization, 'Update peel visualization')
        Publisher.subscribe(self.UpdateEfieldVisualization, 'Update e-field visualization')
        Publisher.subscribe(self.EnableACT, 'Enable ACT')
        Publisher.subscribe(self.UpdateACTData, 'Update ACT data')
        Publisher.subscribe(self.UpdateNavigationStatus, 'Navigation status')
        Publisher.subscribe(self.UpdateTarget, 'Update target')
        Publisher.subscribe(self.OnStartNavigation, 'Start navigation')
        Publisher.subscribe(self.OnStopNavigation, 'Stop navigation')

    def LoadImageFiducials(self, label, position):
        fiducial = self.GetFiducialByAttribute(const.IMAGE_FIDUCIALS, 'label', label)

        fiducial_index = fiducial['fiducial_index']
        fiducial_name = fiducial['fiducial_name']

        if self.btns_set_fiducial[fiducial_index].GetValue():
            print("Fiducial {} already set, not resetting".format(label))
            return

        Publisher.sendMessage('Set image fiducial', fiducial_name=fiducial_name, position=position)

        self.btns_set_fiducial[fiducial_index].SetValue(True)
        for m in [0, 1, 2]:
            self.numctrls_fiducial[fiducial_index][m].SetValue(position[m])

    def GetFiducialByAttribute(self, fiducials, attribute_name, attribute_value):
        found = [fiducial for fiducial in fiducials if fiducial[attribute_name] == attribute_value]

        assert len(found) != 0, "No fiducial found for which {} = {}".format(attribute_name, attribute_value)
        return found[0]

    def SetImageFiducial(self, fiducial_name, position):
        fiducial = self.GetFiducialByAttribute(const.IMAGE_FIDUCIALS, 'fiducial_name', fiducial_name)
        fiducial_index = fiducial['fiducial_index']

        self.image.SetImageFiducial(fiducial_index, position)

    def SetTrackerFiducial(self, fiducial_name):
        if not self.tracker.IsTrackerInitialized():
            dlg.ShowNavigationTrackerWarning(0, 'choose')
            return

        fiducial = self.GetFiducialByAttribute(const.TRACKER_FIDUCIALS, 'fiducial_name', fiducial_name)
        fiducial_index = fiducial['fiducial_index']

        # XXX: The reference mode is fetched from navigation object, however it seems like not quite
        #      navigation-related attribute here, as the reference mode used during the fiducial registration
        #      is more concerned with the calibration than the navigation.
        #
        ref_mode_id = self.navigation.GetReferenceMode()
        self.tracker.SetTrackerFiducial(ref_mode_id, fiducial_index)

        self.ResetICP()
        self.tracker.UpdateUI(self.select_tracker_elem, self.numctrls_fiducial[3:6], self.txtctrl_fre)

    def UpdatePeelVisualization(self, data):
        self.navigation.peel_loaded = data

    def UpdateEfieldVisualization(self, data):
        self.navigation.e_field_loaded = data

    def UpdateNavigationStatus(self, nav_status, vis_status):
        self.nav_status = nav_status
        if nav_status and self.icp.m_icp is not None:
            self.checkbox_icp.Enable(True)
        else:
            self.checkbox_icp.Enable(False)

    def UpdateTrekkerObject(self, data):
        # self.trk_inp = data
        self.navigation.trekker = data

    def UpdateNumTracts(self, data):
        self.navigation.n_tracts = data

    def UpdateSeedOffset(self, data):
        self.navigation.seed_offset = data

    def UpdateSeedRadius(self, data):
        self.navigation.seed_radius = data

    def UpdateSleep(self, data):
        self.navigation.UpdateSleep(data)

    def UpdateNumberThreads(self, data):
        self.navigation.n_threads = data

    def UpdateTractsVisualization(self, data):
        self.navigation.view_tracts = data

    def UpdateACTData(self, data):
        self.navigation.act_data = data

    def UpdateTarget(self, coord):
        self.navigation.target = coord

        if coord is not None:
            self.lock_to_target_checkbox.Enable(True)
            self.lock_to_target_checkbox.SetValue(True)
            self.navigation.SetLockToTarget(True)

    def EnableACT(self, data):
        self.navigation.enable_act = data

    def UpdateImageCoordinates(self, position):
        # TODO: Change from world coordinates to matrix coordinates. They are better for multi software communication.
        self.current_coord = position

        for m in [0, 1, 2]:
            if not self.btns_set_fiducial[m].GetValue():
                for n in [0, 1, 2]:
                    self.numctrls_fiducial[m][n].SetValue(float(position[n]))

    def ResetICP(self):
        self.icp.ResetICP()
        self.checkbox_icp.Enable(False)
        self.checkbox_icp.SetValue(False)

    def DisconnectTracker(self):
        self.tracker.DisconnectTracker()
        self.robot.DisconnectRobot()
        self.ResetICP()
        self.tracker.UpdateUI(self.select_tracker_elem, self.numctrls_fiducial[3:6], self.txtctrl_fre)

    def OnLockToTargetCheckbox(self, evt, ctrl):
        value = ctrl.GetValue()
        self.navigation.SetLockToTarget(value)

    def OnChooseTracker(self, evt, ctrl):
        Publisher.sendMessage('Update status text in GUI',
                              label=_("Configuring tracker ..."))
        if hasattr(evt, 'GetSelection'):
            choice = evt.GetSelection()
        else:
            choice = None

        self.DisconnectTracker()
        self.tracker.ResetTrackerFiducials()
        self.tracker.SetTracker(choice)

        # If 'robot tracker' was selected, configure and initialize robot.
        if self.tracker.tracker_id == const.ROBOT:
            success = self.robot.ConfigureRobot()
            if success:
                self.robot.InitializeRobot()
            else:
                self.DisconnectTracker()

        # XXX: This could be refactored so that all these attributes from this class wouldn't be passed
        #   onto tracker object. (If tracker needs them, maybe at least some of them should be attributes of
        #   Tracker class.)
        self.tracker.UpdateUI(self.select_tracker_elem, self.numctrls_fiducial[3:6], self.txtctrl_fre)

        Publisher.sendMessage('Update status text in GUI', label=_("Ready"))

    def OnChooseReferenceMode(self, evt, ctrl):
        self.navigation.SetReferenceMode(evt.GetSelection())

        # When ref mode is changed the tracker coordinates are set to zero
        self.tracker.ResetTrackerFiducials()

        # Some trackers do not accept restarting within this time window
        # TODO: Improve the restarting of trackers after changing reference mode

        self.ResetICP()

        print("Reference mode changed!")

    def OnImageFiducials(self, n, evt):
        fiducial_name = const.IMAGE_FIDUCIALS[n]['fiducial_name']

        # XXX: This is still a bit hard to read, could be cleaned up.
        label = list(const.BTNS_IMG_MARKERS[evt.GetId()].values())[0]

        if self.btns_set_fiducial[n].GetValue():
            position = self.numctrls_fiducial[n][0].GetValue(),\
                    self.numctrls_fiducial[n][1].GetValue(),\
                    self.numctrls_fiducial[n][2].GetValue()
            orientation = None, None, None

            Publisher.sendMessage('Set image fiducial', fiducial_name=fiducial_name, position=position)

            colour = (0., 1., 0.)
            size = 2
            seed = 3 * [0.]

            Publisher.sendMessage('Create marker', position=position, orientation=orientation, colour=colour, size=size,
                                   label=label, seed=seed)
        else:
            for m in [0, 1, 2]:
                self.numctrls_fiducial[n][m].SetValue(float(self.current_coord[m]))

            Publisher.sendMessage('Set image fiducial', fiducial_name=fiducial_name, position=np.nan)
            Publisher.sendMessage('Delete fiducial marker', label=label)

    def OnTrackerFiducials(self, n, evt, ctrl):

        # Do not allow several tracker fiducials to be set at the same time.
        if self.tracker_fiducial_being_set is not None and self.tracker_fiducial_being_set != n:
            ctrl.SetValue(False)
            return

        # Called when the button for setting the tracker fiducial is enabled and either pedal is pressed
        # or the button is pressed again.
        #
        def set_fiducial_callback(state):
            if state:
                fiducial_name = const.TRACKER_FIDUCIALS[n]['fiducial_name']
                Publisher.sendMessage('Set tracker fiducial', fiducial_name=fiducial_name)

                ctrl.SetValue(False)
                self.tracker_fiducial_being_set = None

        if ctrl.GetValue():
            self.tracker_fiducial_being_set = n

            if self.pedal_connection is not None:
                self.pedal_connection.add_callback(
                    name='fiducial',
                    callback=set_fiducial_callback,
                    remove_when_released=True,
                )

            if self.neuronavigation_api is not None:
                self.neuronavigation_api.add_pedal_callback(
                    name='fiducial',
                    callback=set_fiducial_callback,
                    remove_when_released=True,
                )
        else:
            set_fiducial_callback(True)

            if self.pedal_connection is not None:
                self.pedal_connection.remove_callback(name='fiducial')

            if self.neuronavigation_api is not None:
                self.neuronavigation_api.remove_pedal_callback(name='fiducial')

    def OnStopNavigation(self):
        select_tracker_elem = self.select_tracker_elem
        choice_ref = self.choice_ref

        self.navigation.StopNavigation()
        if self.tracker.tracker_id == const.ROBOT:
            Publisher.sendMessage('Update robot target', robot_tracker_flag=False,
                                  target_index=None, target=None)

        # Enable all navigation buttons
        choice_ref.Enable(True)
        select_tracker_elem.Enable(True)

        for btn_c in self.btns_set_fiducial:
            btn_c.Enable(True)

    def CheckFiducialRegistrationError(self):
        self.navigation.UpdateFiducialRegistrationError(self.tracker, self.image)
        fre, fre_ok = self.navigation.GetFiducialRegistrationError(self.icp)

        self.txtctrl_fre.SetValue(str(round(fre, 2)))
        if fre_ok:
            self.txtctrl_fre.SetBackgroundColour('GREEN')
        else:
            self.txtctrl_fre.SetBackgroundColour('RED')

        return fre_ok

    def OnStartNavigation(self):
        select_tracker_elem = self.select_tracker_elem
        choice_ref = self.choice_ref

        if not self.tracker.AreTrackerFiducialsSet() or not self.image.AreImageFiducialsSet():
            wx.MessageBox(_("Invalid fiducials, select all coordinates."), _("InVesalius 3"))

        elif not self.tracker.IsTrackerInitialized():
            dlg.ShowNavigationTrackerWarning(0, 'choose')
            errors = True

        else:
            # Prepare GUI for navigation.
            Publisher.sendMessage("Toggle Cross", id=const.SLICE_STATE_CROSS)
            Publisher.sendMessage("Hide current mask")

            # Disable all navigation buttons.
            choice_ref.Enable(False)
            select_tracker_elem.Enable(False)
            for btn_c in self.btns_set_fiducial:
                btn_c.Enable(False)

            self.navigation.EstimateTrackerToInVTransformationMatrix(self.tracker, self.image)

            if not self.CheckFiducialRegistrationError():
                # TODO: Exhibit FRE in a warning dialog and only starts navigation after user clicks ok
                print("WARNING: Fiducial registration error too large.")

            self.icp.RegisterICP(self.navigation, self.tracker)
            if self.icp.use_icp:
                self.checkbox_icp.Enable(True)
                self.checkbox_icp.SetValue(True)
                # Update FRE once more after starting the navigation, due to the optional use of ICP,
                # which improves FRE.
                self.CheckFiducialRegistrationError()

            self.navigation.StartNavigation(self.tracker, self.icp)

    def OnNavigate(self, evt, btn_nav):
        select_tracker_elem = self.select_tracker_elem
        choice_ref = self.choice_ref

        nav_id = btn_nav.GetValue()
        if not nav_id:
            wx.CallAfter(Publisher.sendMessage, 'Stop navigation')

            tooltip = wx.ToolTip(_("Start neuronavigation"))
            btn_nav.SetToolTip(tooltip)
        else:
            Publisher.sendMessage("Start navigation")

            if self.nav_status:
                tooltip = wx.ToolTip(_("Stop neuronavigation"))
                btn_nav.SetToolTip(tooltip)
            else:
                btn_nav.SetValue(False)

    def ResetUI(self):
        for m in range(0, 3):
            self.btns_set_fiducial[m].SetValue(False)
            for n in range(0, 3):
                self.numctrls_fiducial[m][n].SetValue(0.0)

    def OnCheckboxICP(self, evt, ctrl):
        self.icp.SetICP(self.navigation, ctrl.GetValue())
        self.CheckFiducialRegistrationError()

    def OnCloseProject(self):
        self.ResetUI()
        Publisher.sendMessage('Disconnect tracker')
        Publisher.sendMessage('Update object registration')
        Publisher.sendMessage('Show and track coil', enabled=False)
        Publisher.sendMessage('Delete all markers')
        Publisher.sendMessage("Update marker offset state", create=False)
        Publisher.sendMessage("Remove tracts")
        Publisher.sendMessage("Set cross visibility", visibility=0)
        # TODO: Reset camera initial focus
        Publisher.sendMessage('Reset cam clipping range')
        self.navigation.StopNavigation()
        self.navigation.__init__(
            pedal_connection=self.pedal_connection,
            neuronavigation_api=self.neuronavigation_api
        )
        self.tracker.__init__()
        self.icp.__init__()


class ObjectRegistrationPanel(wx.Panel):
    def __init__(self, parent, tracker, pedal_connection, neuronavigation_api):
        wx.Panel.__init__(self, parent)
        try:
            default_colour = wx.SystemSettings.GetColour(wx.SYS_COLOUR_MENUBAR)
        except AttributeError:
            default_colour = wx.SystemSettings_GetColour(wx.SYS_COLOUR_MENUBAR)
        self.SetBackgroundColour(default_colour)

        self.coil_list = const.COIL

        self.tracker = tracker
        self.pedal_connection = pedal_connection
        self.neuronavigation_api = neuronavigation_api

        self.nav_prop = None
        self.obj_fiducials = None
        self.obj_orients = None
        self.obj_ref_mode = None
        self.obj_name = None
        self.timestamp = const.TIMESTAMP

        self.SetAutoLayout(1)
        self.__bind_events()

        # Button for creating new coil
        tooltip = wx.ToolTip(_("Create new coil"))
        btn_new = wx.Button(self, -1, _("New"), size=wx.Size(65, 23))
        btn_new.SetToolTip(tooltip)
        btn_new.Enable(1)
        btn_new.Bind(wx.EVT_BUTTON, self.OnCreateNewCoil)
        self.btn_new = btn_new

        # Button for loading coil config file
        tooltip = wx.ToolTip(_("Load coil configuration file"))
        btn_load = wx.Button(self, -1, _("Load"), size=wx.Size(65, 23))
        btn_load.SetToolTip(tooltip)
        btn_load.Enable(1)
        btn_load.Bind(wx.EVT_BUTTON, self.OnLoadCoil)
        self.btn_load = btn_load

        # Save button for saving coil config file
        tooltip = wx.ToolTip(_(u"Save coil configuration file"))
        btn_save = wx.Button(self, -1, _(u"Save"), size=wx.Size(65, 23))
        btn_save.SetToolTip(tooltip)
        btn_save.Enable(1)
        btn_save.Bind(wx.EVT_BUTTON, self.OnSaveCoil)
        self.btn_save = btn_save

        # Create a horizontal sizer to represent button save
        line_save = wx.BoxSizer(wx.HORIZONTAL)
        line_save.Add(btn_new, 1, wx.LEFT | wx.TOP | wx.RIGHT, 4)
        line_save.Add(btn_load, 1, wx.LEFT | wx.TOP | wx.RIGHT, 4)
        line_save.Add(btn_save, 1, wx.LEFT | wx.TOP | wx.RIGHT, 4)

        # Change angles threshold
        text_angles = wx.StaticText(self, -1, _("Angle threshold [degrees]:"))
        spin_size_angles = wx.SpinCtrlDouble(self, -1, "", size=wx.Size(50, 23))
        spin_size_angles.SetRange(0.1, 99)
        spin_size_angles.SetValue(const.COIL_ANGLES_THRESHOLD)
        spin_size_angles.Bind(wx.EVT_TEXT, partial(self.OnSelectAngleThreshold, ctrl=spin_size_angles))
        spin_size_angles.Bind(wx.EVT_SPINCTRL, partial(self.OnSelectAngleThreshold, ctrl=spin_size_angles))

        # Change dist threshold
        text_dist = wx.StaticText(self, -1, _("Distance threshold [mm]:"))
        spin_size_dist = wx.SpinCtrlDouble(self, -1, "", size=wx.Size(50, 23))
        spin_size_dist.SetRange(0.1, 99)
        spin_size_dist.SetValue(const.COIL_ANGLES_THRESHOLD)
        spin_size_dist.Bind(wx.EVT_TEXT, partial(self.OnSelectDistThreshold, ctrl=spin_size_dist))
        spin_size_dist.Bind(wx.EVT_SPINCTRL, partial(self.OnSelectDistThreshold, ctrl=spin_size_dist))

        # Change timestamp interval
        text_timestamp = wx.StaticText(self, -1, _("Timestamp interval [s]:"))
        spin_timestamp_dist = wx.SpinCtrlDouble(self, -1, "", size=wx.Size(50, 23), inc = 0.1)
        spin_timestamp_dist.SetRange(0.5, 60.0)
        spin_timestamp_dist.SetValue(self.timestamp)
        spin_timestamp_dist.Bind(wx.EVT_TEXT, partial(self.OnSelectTimestamp, ctrl=spin_timestamp_dist))
        spin_timestamp_dist.Bind(wx.EVT_SPINCTRL, partial(self.OnSelectTimestamp, ctrl=spin_timestamp_dist))
        self.spin_timestamp_dist = spin_timestamp_dist

        # Create a horizontal sizer to threshold configs
        line_angle_threshold = wx.BoxSizer(wx.HORIZONTAL)
        line_angle_threshold.AddMany([(text_angles, 1, wx.EXPAND | wx.GROW | wx.TOP| wx.RIGHT | wx.LEFT, 5),
                                      (spin_size_angles, 0, wx.ALL | wx.EXPAND | wx.GROW, 5)])

        line_dist_threshold = wx.BoxSizer(wx.HORIZONTAL)
        line_dist_threshold.AddMany([(text_dist, 1, wx.EXPAND | wx.GROW | wx.TOP| wx.RIGHT | wx.LEFT, 5),
                                      (spin_size_dist, 0, wx.ALL | wx.EXPAND | wx.GROW, 5)])

        line_timestamp = wx.BoxSizer(wx.HORIZONTAL)
        line_timestamp.AddMany([(text_timestamp, 1, wx.EXPAND | wx.GROW | wx.TOP| wx.RIGHT | wx.LEFT, 5),
                                      (spin_timestamp_dist, 0, wx.ALL | wx.EXPAND | wx.GROW, 5)])

        # Check box for trigger monitoring to create markers from serial port
        checkrecordcoords = wx.CheckBox(self, -1, _('Record coordinates'))
        checkrecordcoords.SetValue(False)
        checkrecordcoords.Enable(0)
        checkrecordcoords.Bind(wx.EVT_CHECKBOX, partial(self.OnRecordCoords, ctrl=checkrecordcoords))
        self.checkrecordcoords = checkrecordcoords

        # Check box to track object or simply the stylus
        checkbox_track_object = wx.CheckBox(self, -1, _('Track object'))
        checkbox_track_object.SetValue(False)
        checkbox_track_object.Enable(0)
        checkbox_track_object.Bind(wx.EVT_CHECKBOX, partial(self.OnTrackObjectCheckbox, ctrl=checkbox_track_object))
        self.checkbox_track_object = checkbox_track_object

        line_checks = wx.BoxSizer(wx.HORIZONTAL)
        line_checks.Add(checkrecordcoords, 0, wx.ALIGN_LEFT | wx.RIGHT | wx.LEFT, 5)
        line_checks.Add(checkbox_track_object, 0, wx.RIGHT | wx.LEFT, 5)

        # Add line sizers into main sizer
        main_sizer = wx.BoxSizer(wx.VERTICAL)
        main_sizer.Add(line_save, 0, wx.LEFT | wx.RIGHT | wx.TOP | wx.ALIGN_CENTER_HORIZONTAL, 5)
        main_sizer.Add(line_angle_threshold, 0, wx.GROW | wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 5)
        main_sizer.Add(line_dist_threshold, 0, wx.GROW | wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 5)
        main_sizer.Add(line_timestamp, 0, wx.GROW | wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 5)
        main_sizer.Add(line_checks, 0, wx.GROW | wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP | wx.BOTTOM, 10)
        main_sizer.Fit(self)

        self.SetSizer(main_sizer)
        self.Update()

        self.LoadState()

    def __bind_events(self):
        Publisher.subscribe(self.UpdateNavigationStatus, 'Navigation status')
        Publisher.subscribe(self.OnCloseProject, 'Close project data')
        Publisher.subscribe(self.OnRemoveObject, 'Remove object data')

        # Externally check/uncheck and enable/disable checkboxes.
        Publisher.subscribe(self.CheckTrackObjectCheckbox, 'Check track-object checkbox')
        Publisher.subscribe(self.EnableTrackObjectCheckbox, 'Enable track-object checkbox')

    def SaveState(self):
        track_object = self.checkbox_track_object
        state = {
            'track_object': {
                'checked': track_object.IsChecked(),
                'enabled': track_object.IsEnabled(),
            }
        }

        session = ses.Session()
        session.SetState('object_registration_panel', state)

    def LoadState(self):
        session = ses.Session()
        state = session.GetState('object_registration_panel')

        if state is None:
            return

        track_object = state['track_object']

        self.EnableTrackObjectCheckbox(track_object['enabled'])
        self.CheckTrackObjectCheckbox(track_object['checked'])

    def UpdateNavigationStatus(self, nav_status, vis_status):
        if nav_status:
            self.checkrecordcoords.Enable(1)
            self.checkbox_track_object.Enable(0)
            self.btn_save.Enable(0)
            self.btn_new.Enable(0)
            self.btn_load.Enable(0)
        else:
            self.OnRecordCoords(nav_status, self.checkrecordcoords)
            self.checkrecordcoords.SetValue(False)
            self.checkrecordcoords.Enable(0)
            self.btn_save.Enable(1)
            self.btn_new.Enable(1)
            self.btn_load.Enable(1)
            if self.obj_fiducials is not None:
                self.checkbox_track_object.Enable(1)
                #Publisher.sendMessage('Enable target button', True)

    def OnSelectAngleThreshold(self, evt, ctrl):
        Publisher.sendMessage('Update angle threshold', angle=ctrl.GetValue())

    def OnSelectDistThreshold(self, evt, ctrl):
        Publisher.sendMessage('Update dist threshold', dist_threshold=ctrl.GetValue())

    def OnSelectTimestamp(self, evt, ctrl):
        self.timestamp = ctrl.GetValue()

    def OnRecordCoords(self, evt, ctrl):
        if ctrl.GetValue() and evt:
            self.spin_timestamp_dist.Enable(0)
            self.thr_record = rec.Record(ctrl.GetValue(), self.timestamp)
        elif (not ctrl.GetValue() and evt) or (ctrl.GetValue() and not evt) :
            self.spin_timestamp_dist.Enable(1)
            self.thr_record.stop()
        elif not ctrl.GetValue() and not evt:
            None

    # 'Track object' checkbox

    def EnableTrackObjectCheckbox(self, enabled):
        self.checkbox_track_object.Enable(enabled)

    def CheckTrackObjectCheckbox(self, checked):
        self.checkbox_track_object.SetValue(checked)
        self.OnTrackObjectCheckbox()

    def OnTrackObjectCheckbox(self, evt=None, ctrl=None):
        checked = self.checkbox_track_object.IsChecked()
        Publisher.sendMessage('Track object', enabled=checked)

        # Disable or enable 'Show coil' checkbox, based on if 'Track object' checkbox is checked.
        Publisher.sendMessage('Enable show-coil checkbox', enabled=checked)

        # Also, automatically check or uncheck 'Show coil' checkbox.
        Publisher.sendMessage('Check show-coil checkbox', checked=checked)

        self.SaveState()

    def OnComboCoil(self, evt):
        # coil_name = evt.GetString()
        coil_index = evt.GetSelection()
        Publisher.sendMessage('Change selected coil', self.coil_list[coil_index][1])

    def OnCreateNewCoil(self, event=None):
        if self.tracker.IsTrackerInitialized():
            dialog = dlg.ObjectCalibrationDialog(self.tracker, self.pedal_connection, self.neuronavigation_api)
            try:
                if dialog.ShowModal() == wx.ID_OK:
                    self.obj_fiducials, self.obj_orients, self.obj_ref_mode, self.obj_name, polydata, use_default_object = dialog.GetValue()

                    self.neuronavigation_api.update_coil_mesh(polydata)

                    if np.isfinite(self.obj_fiducials).all() and np.isfinite(self.obj_orients).all():
                        Publisher.sendMessage('Update object registration',
                                              data=(self.obj_fiducials, self.obj_orients, self.obj_ref_mode, self.obj_name))
                        Publisher.sendMessage('Update status text in GUI',
                                              label=_("Ready"))
                        Publisher.sendMessage(
                            'Configure object',
                            obj_name=self.obj_name,
                            polydata=polydata,
                            use_default_object=use_default_object,
                        )

                        # Automatically enable and check 'Track object' checkbox and uncheck 'Disable Volume Camera' checkbox.
                        Publisher.sendMessage('Enable track-object checkbox', enabled=True)
                        Publisher.sendMessage('Check track-object checkbox', checked=True)
                        Publisher.sendMessage('Check volume camera checkbox', checked=False)

                        Publisher.sendMessage('Disable target mode')

            except wx._core.PyAssertionError:  # TODO FIX: win64
                pass
            dialog.Destroy()
        else:
            dlg.ShowNavigationTrackerWarning(0, 'choose')

    def OnLoadCoil(self, event=None):
        filename = dlg.ShowLoadSaveDialog(message=_(u"Load object registration"),
                                          wildcard=_("Registration files (*.obr)|*.obr"))
        # data_dir = os.environ.get('OneDrive') + r'\data\dti_navigation\baran\anat_reg_improve_20200609'
        # coil_path = 'magstim_coil_dell_laptop.obr'
        # filename = os.path.join(data_dir, coil_path)

        try:
            if filename:
                with open(filename, 'r') as text_file:
                    data = [s.split('\t') for s in text_file.readlines()]

                registration_coordinates = np.array(data[1:]).astype(np.float32)
                self.obj_fiducials = registration_coordinates[:, :3]
                self.obj_orients = registration_coordinates[:, 3:]

                self.obj_name = data[0][1].encode(const.FS_ENCODE)
                self.obj_ref_mode = int(data[0][-1])

                if not os.path.exists(self.obj_name):
                    self.obj_name = os.path.join(inv_paths.OBJ_DIR, "magstim_fig8_coil.stl")

                polydata = vtk_utils.CreateObjectPolyData(self.obj_name)
                if polydata:
                    self.neuronavigation_api.update_coil_mesh(polydata)
                else:
                    self.obj_name = os.path.join(inv_paths.OBJ_DIR, "magstim_fig8_coil.stl")

                if os.path.basename(self.obj_name) == "magstim_fig8_coil.stl":
                    use_default_object = True
                else:
                    use_default_object = False

                Publisher.sendMessage('Update object registration',
                                      data=(self.obj_fiducials, self.obj_orients, self.obj_ref_mode, self.obj_name))
                Publisher.sendMessage('Update status text in GUI',
                                      label=_("Object file successfully loaded"))
                Publisher.sendMessage(
                    'Configure object',
                    obj_name=self.obj_name,
                    polydata=polydata,
                    use_default_object=use_default_object
                )

                # Automatically enable and check 'Track object' checkbox and uncheck 'Disable Volume Camera' checkbox.
                Publisher.sendMessage('Enable track-object checkbox', enabled=True)
                Publisher.sendMessage('Check track-object checkbox', checked=True)
                Publisher.sendMessage('Check volume camera checkbox', checked=False)

                Publisher.sendMessage('Disable target mode')
                if use_default_object:
                    msg = _("Default object file successfully loaded")
                else:
                    msg = _("Object file successfully loaded")
                wx.MessageBox(msg, _("InVesalius 3"))
        except:
            wx.MessageBox(_("Object registration file incompatible."), _("InVesalius 3"))
            Publisher.sendMessage('Update status text in GUI', label="")

    def OnSaveCoil(self, evt):
        if np.isnan(self.obj_fiducials).any() or np.isnan(self.obj_orients).any():
            wx.MessageBox(_("Digitize all object fiducials before saving"), _("Save error"))
        else:
            filename = dlg.ShowLoadSaveDialog(message=_(u"Save object registration as..."),
                                              wildcard=_("Registration files (*.obr)|*.obr"),
                                              style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
                                              default_filename="object_registration.obr", save_ext="obr")
            if filename:
                hdr = 'Object' + "\t" + utils.decode(self.obj_name, const.FS_ENCODE) + "\t" + 'Reference' + "\t" + str('%d' % self.obj_ref_mode)
                data = np.hstack([self.obj_fiducials, self.obj_orients])
                np.savetxt(filename, data, fmt='%.4f', delimiter='\t', newline='\n', header=hdr)
                wx.MessageBox(_("Object file successfully saved"), _("Save"))

    def OnCloseProject(self):
        self.OnRemoveObject()

    def OnRemoveObject(self):
        self.checkrecordcoords.SetValue(False)
        self.checkrecordcoords.Enable(0)
        self.checkbox_track_object.SetValue(False)
        self.checkbox_track_object.Enable(0)

        self.nav_prop = None
        self.obj_fiducials = None
        self.obj_orients = None
        self.obj_ref_mode = None
        self.obj_name = None
        self.timestamp = const.TIMESTAMP


class MarkersPanel(wx.Panel):
    @dataclasses.dataclass
    class Marker:
        """Class for storing markers. @dataclass decorator simplifies
        setting default values, serialization, etc."""
        x : float = 0
        y : float = 0
        z : float = 0
        alpha : float = dataclasses.field(default = None)
        beta : float = dataclasses.field(default = None)
        gamma : float = dataclasses.field(default = None)
        r : float = 0
        g : float = 1
        b : float = 0
        size : float = 2
        label : str = '*'
        x_seed : float = 0
        y_seed : float = 0
        z_seed : float = 0
        is_target : bool = False
        session_id : int = 1
        is_brain_target : bool = False

        # x, y, z can be jointly accessed as position
        @property
        def position(self):
            return list((self.x, self.y, self.z))

        @position.setter
        def position(self, new_position):
            self.x, self.y, self.z = new_position

        # alpha, beta, gamma can be jointly accessed as orientation
        @property
        def orientation(self):
            return list((self.alpha, self.beta, self.gamma))

        @orientation.setter
        def orientation(self, new_orientation):
            self.alpha, self.beta, self.gamma = new_orientation

        # alpha, beta, gamma can be jointly accessed as orientation
        @property
        def coordinate(self):
            return list((self.x, self.y, self.z, self.alpha, self.beta, self.gamma))

        # r, g, b can be jointly accessed as colour
        @property
        def colour(self):
            return list((self.r, self.g, self.b),)

        @colour.setter
        def colour(self, new_colour):
            self.r, self.g, self.b = new_colour

        # x_seed, y_seed, z_seed can be jointly accessed as seed
        @property
        def seed(self):
            return list((self.x_seed, self.y_seed, self.z_seed),)

        @seed.setter
        def seed(self, new_seed):
            self.x_seed, self.y_seed, self.z_seed = new_seed

        @classmethod
        def to_string_headers(cls):
            """Return the string containing tab-separated list of field names (headers)."""
            res = [field.name for field in dataclasses.fields(cls)]
            res.extend(['x_world', 'y_world', 'z_world', 'alpha_world', 'beta_world', 'gamma_world'])
            return '\t'.join(map(lambda x: '\"%s\"' % x, res))

        def to_string(self):
            """Serialize to excel-friendly tab-separated string"""
            res = ''
            for field in dataclasses.fields(self.__class__):
                if field.type is str:
                    res += ('\"%s\"\t' % getattr(self, field.name))
                else:
                    res += ('%s\t' % str(getattr(self, field.name)))

            if self.alpha is not None and self.beta is not None and self.gamma is not None:
                # Add world coordinates (in addition to the internal ones).
                position_world, orientation_world = imagedata_utils.convert_invesalius_to_world(
                    position=[self.x, self.y, self.z],
                    orientation=[self.alpha, self.beta, self.gamma],
                )

            else:
                position_world, orientation_world = imagedata_utils.convert_invesalius_to_world(
                      position=[self.x, self.y, self.z],
                      orientation=[0,0,0],
                 )

            res += '\t'.join(map(lambda x: 'N/A' if x is None else str(x), (*position_world, *orientation_world)))
            return res

        def from_string(self, inp_str):
            """Deserialize from a tab-separated string. If the string is not 
            properly formatted, might throw an exception and leave the object
            in an inconsistent state."""
            for field, str_val in zip(dataclasses.fields(self.__class__), inp_str.split('\t')):
                if field.type is float and str_val != 'None':
                    setattr(self, field.name, float(str_val))
                if field.type is float and str_val == 'None':
                    setattr(self, field.name, None)
                if field.type is float and str_val != 'None':
                    setattr(self, field.name, float(str_val))
                if field.type is str:
                    setattr(self, field.name, str_val[1:-1]) # remove the quotation marks
                if field.type is bool:
                    setattr(self, field.name, str_val=='True')

        def to_dict(self):
            return {
                'position': self.position,
                'orientation': self.orientation,
                'colour': self.colour,
                'size': self.size,
                'label': self.label,
                'is_target': self.is_target,
                'seed': self.seed,
                'session_id': self.session_id,
            }


    def __init__(self, parent, navigation, tracker, icp):
        wx.Panel.__init__(self, parent)
        try:
            default_colour = wx.SystemSettings.GetColour(wx.SYS_COLOUR_MENUBAR)
        except AttributeError:
            default_colour = wx.SystemSettings_GetColour(wx.SYS_COLOUR_MENUBAR)
        self.SetBackgroundColour(default_colour)

        self.SetAutoLayout(1)

        self.navigation = navigation
        self.tracker = tracker
        self.icp = icp
        if has_mTMS:
            self.mTMS = mTMS()
        else:
            self.mTMS = None

        self.__bind_events()

        self.session = ses.Session()

        self.current_position = [0, 0, 0]
        self.current_orientation = [None, None, None]
        self.current_seed = 0, 0, 0

        self.markers = []
        self.nav_status = False
        self.efield_loaded = False
        self.efield_data_saved = False
        self.efield_target_idx = None
        self.target_mode = False

        self.marker_colour = const.MARKER_COLOUR
        self.marker_size = const.MARKER_SIZE
        self.arrow_marker_size = const.ARROW_MARKER_SIZE
        self.current_session = 1

        self.brain_actor = None
        # Change marker size
        spin_size = wx.SpinCtrl(self, -1, "", size=wx.Size(40, 23))
        spin_size.SetRange(1, 99)
        spin_size.SetValue(self.marker_size)
        spin_size.Bind(wx.EVT_TEXT, partial(self.OnSelectSize, ctrl=spin_size))
        spin_size.Bind(wx.EVT_SPINCTRL, partial(self.OnSelectSize, ctrl=spin_size))

        # Marker colour select
        select_colour = csel.ColourSelect(self, -1, colour=[255*s for s in self.marker_colour], size=wx.Size(20, 23))
        select_colour.Bind(csel.EVT_COLOURSELECT, partial(self.OnSelectColour, ctrl=select_colour))

        btn_create = wx.Button(self, -1, label=_('Create marker'), size=wx.Size(135, 23))
        btn_create.Bind(wx.EVT_BUTTON, self.OnCreateMarker)

        sizer_create = wx.FlexGridSizer(rows=1, cols=3, hgap=5, vgap=5)
        sizer_create.AddMany([(spin_size, 1),
                              (select_colour, 0),
                              (btn_create, 0)])

        # Buttons to save and load markers and to change its visibility as well
        btn_save = wx.Button(self, -1, label=_('Save'), size=wx.Size(65, 23))
        btn_save.Bind(wx.EVT_BUTTON, self.OnSaveMarkers)

        btn_load = wx.Button(self, -1, label=_('Load'), size=wx.Size(65, 23))
        btn_load.Bind(wx.EVT_BUTTON, self.OnLoadMarkers)

        btn_visibility = wx.ToggleButton(self, -1, _("Hide"), size=wx.Size(65, 23))
        btn_visibility.Bind(wx.EVT_TOGGLEBUTTON, partial(self.OnMarkersVisibility, ctrl=btn_visibility))

        sizer_btns = wx.FlexGridSizer(rows=1, cols=3, hgap=5, vgap=5)
        sizer_btns.AddMany([(btn_save, 1, wx.RIGHT),
                            (btn_load, 0, wx.LEFT | wx.RIGHT),
                            (btn_visibility, 0, wx.LEFT)])

        # Buttons to delete or remove markers
        btn_delete_single = wx.Button(self, -1, label=_('Remove'), size=wx.Size(65, 23))
        btn_delete_single.Bind(wx.EVT_BUTTON, self.OnDeleteMultipleMarkers)

        btn_delete_all = wx.Button(self, -1, label=_('Delete all'), size=wx.Size(135, 23))
        btn_delete_all.Bind(wx.EVT_BUTTON, self.OnDeleteAllMarkers)

        sizer_delete = wx.FlexGridSizer(rows=1, cols=2, hgap=5, vgap=5)
        sizer_delete.AddMany([(btn_delete_single, 1, wx.RIGHT),
                              (btn_delete_all, 0, wx.LEFT)])

        # List of markers
        marker_list_ctrl = wx.ListCtrl(self, -1, style=wx.LC_REPORT, size=wx.Size(0,120))
        marker_list_ctrl.InsertColumn(const.ID_COLUMN, '#')
        marker_list_ctrl.SetColumnWidth(const.ID_COLUMN, 28)

        marker_list_ctrl.InsertColumn(const.SESSION_COLUMN, 'Session')
        marker_list_ctrl.SetColumnWidth(const.SESSION_COLUMN, 52)

        marker_list_ctrl.InsertColumn(const.LABEL_COLUMN, 'Label')
        marker_list_ctrl.SetColumnWidth(const.LABEL_COLUMN, 118)

        marker_list_ctrl.InsertColumn(const.TARGET_COLUMN, 'Target')
        marker_list_ctrl.SetColumnWidth(const.TARGET_COLUMN, 45)

        if self.session.GetConfig('debug'):
            marker_list_ctrl.InsertColumn(const.X_COLUMN, 'X')
            marker_list_ctrl.SetColumnWidth(const.X_COLUMN, 45)

            marker_list_ctrl.InsertColumn(const.Y_COLUMN, 'Y')
            marker_list_ctrl.SetColumnWidth(const.Y_COLUMN, 45)

            marker_list_ctrl.InsertColumn(const.Z_COLUMN, 'Z')
            marker_list_ctrl.SetColumnWidth(const.Z_COLUMN, 45)

        marker_list_ctrl.Bind(wx.EVT_LIST_ITEM_RIGHT_CLICK, self.OnMouseRightDown)
        marker_list_ctrl.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.OnItemBlink)
        marker_list_ctrl.Bind(wx.EVT_LIST_ITEM_DESELECTED, self.OnStopItemBlink)

        self.marker_list_ctrl = marker_list_ctrl

        # Add all lines into main sizer
        group_sizer = wx.BoxSizer(wx.VERTICAL)
        group_sizer.Add(sizer_create, 0, wx.TOP | wx.BOTTOM | wx.ALIGN_CENTER_HORIZONTAL, 5)
        group_sizer.Add(sizer_btns, 0, wx.BOTTOM | wx.ALIGN_CENTER_HORIZONTAL, 5)
        group_sizer.Add(sizer_delete, 0, wx.BOTTOM | wx.ALIGN_CENTER_HORIZONTAL, 5)
        group_sizer.Add(marker_list_ctrl, 0, wx.EXPAND | wx.ALL, 5)
        group_sizer.Fit(self)

        self.SetSizer(group_sizer)
        self.Update()

        self.LoadState()

    def __bind_events(self):
        Publisher.subscribe(self.UpdateCurrentCoord, 'Set cross focal point')
        Publisher.subscribe(self.OnDeleteMultipleMarkers, 'Delete fiducial marker')
        Publisher.subscribe(self.OnDeleteAllMarkers, 'Delete all markers')
        Publisher.subscribe(self.CreateMarker, 'Create marker')
        Publisher.subscribe(self.SetMarkers, 'Set markers')
        Publisher.subscribe(self.UpdateNavigationStatus, 'Navigation status')
        Publisher.subscribe(self.UpdateSeedCoordinates, 'Update tracts')
        Publisher.subscribe(self.OnChangeCurrentSession, 'Current session changed')
        Publisher.subscribe(self.UpdateMarkerOrientation, 'Open marker orientation dialog')
        Publisher.subscribe(self.OnActivateTargetMode, 'Target navigation mode')
        Publisher.subscribe(self.AddPeeledSurface, 'Update peel')
        Publisher.subscribe(self.GetEfieldDataStatus, 'Get status of Efield saved data')
        Publisher.subscribe(self.GetIdList, 'Get ID list')
        Publisher.subscribe(self.GetRotationPosition, 'Send coil position and rotation')
    def SaveState(self):
        state = [marker.to_dict() for marker in self.markers]

        session = ses.Session()
        session.SetState('markers', state)

    def LoadState(self):
        session = ses.Session()
        state = session.GetState('markers')

        if state is None:
            return

        for d in state:
            self.CreateMarker(
                position=d['position'],
                orientation=d['orientation'],
                colour=d['colour'],
                size=d['size'],
                label=d['label'],
                # XXX: See comment below. Should be improved so that is_target wouldn't need to be set as False here.
                is_target=False,
                seed=d['seed'],
                session_id=d['session_id']
            )
            # XXX: Do the same thing as in OnLoadMarkers function: first create marker that is never set as a target,
            # then set as target if needed. This could be refactored so that a CreateMarker call would
            # suffice to set it as target.
            if d['is_target']:
                self.__set_marker_as_target(len(self.markers) - 1, display_messagebox=False)

    def __find_target_marker(self):
        """
        Return the index of the marker currently selected as target (there
        should be at most one). If there is no such marker, return None.
        """
        for i in range(len(self.markers)):
            if self.markers[i].is_target:
                return i
                
        return None

    def __get_brain_target_markers(self):
        """
        Return the index of the marker currently selected as target (there
        should be at most one). If there is no such marker, return None.
        """
        brain_target_list = []
        for i in range(len(self.markers)):
            if self.markers[i].is_brain_target:
                brain_target_list.append(self.markers[i].coordinate)
        if brain_target_list:
            return brain_target_list

        return None

    def __get_selected_items(self):
        """    
        Returns a (possibly empty) list of the selected items in the list control.
        """
        selection = []

        next = self.marker_list_ctrl.GetFirstSelected()

        while next != -1:
            selection.append(next)
            next = self.marker_list_ctrl.GetNextSelected(next)

        return selection

    def __delete_all_markers(self):
        """
        Delete all markers
        """
        for i in reversed(range(len(self.markers))):
            del self.markers[i]
            self.marker_list_ctrl.DeleteItem(i)

    def __delete_multiple_markers(self, indexes):
        """
        Delete multiple markers indexed by 'indexes'. Indexes must be sorted in
        the ascending order.
        """
        for i in reversed(indexes):
            del self.markers[i]
            self.marker_list_ctrl.DeleteItem(i)
            for n in range(0, self.marker_list_ctrl.GetItemCount()):
                self.marker_list_ctrl.SetItem(n, 0, str(n + 1))

        Publisher.sendMessage('Remove multiple markers', indexes=indexes)

    def __delete_all_brain_targets(self):
        """
        Delete all brain targets markers
        """
        brain_target_index = []
        for index in range(len(self.markers)):
            if self.markers[index].is_brain_target:
                brain_target_index.append(index)
        for index in reversed(brain_target_index):
            self.marker_list_ctrl.SetItemBackgroundColour(index, 'white')
            del self.markers[index]
            self.marker_list_ctrl.DeleteItem(index)
            for n in range(0, self.marker_list_ctrl.GetItemCount()):
                self.marker_list_ctrl.SetItem(n, 0, str(n + 1))
        Publisher.sendMessage('Remove multiple markers', indexes=brain_target_index)

    def __set_marker_as_target(self, idx, display_messagebox=True):
        """
        Set marker indexed by idx as the new target. idx must be a valid index.
        """
        # Find the previous target
        prev_idx = self.__find_target_marker()

        # If the new target is same as the previous do nothing.
        if prev_idx == idx:
            return

        # Unset the previous target
        if prev_idx is not None:
            self.markers[prev_idx].is_target = False
            self.marker_list_ctrl.SetItemBackgroundColour(prev_idx, 'white')
            Publisher.sendMessage('Set target transparency', status=False, index=prev_idx)
            self.marker_list_ctrl.SetItem(prev_idx, const.TARGET_COLUMN, "")

        # Set the new target
        self.markers[idx].is_target = True
        self.marker_list_ctrl.SetItemBackgroundColour(idx, 'RED')
        self.marker_list_ctrl.SetItem(idx, const.TARGET_COLUMN, _("Yes"))

        Publisher.sendMessage('Update target', coord=self.markers[idx].position+self.markers[idx].orientation)
        Publisher.sendMessage('Set target transparency', status=True, index=idx)
        #self.__delete_all_brain_targets()
        if display_messagebox:
            wx.MessageBox(_("New target selected."), _("InVesalius 3"))

    @staticmethod
    def __list_fiducial_labels():
        """Return the list of marker labels denoting fiducials."""
        return list(itertools.chain(*(const.BTNS_IMG_MARKERS[i].values() for i in const.BTNS_IMG_MARKERS)))

    def UpdateCurrentCoord(self, position):
        self.current_position = list(position[:3])
        self.current_orientation = list(position[3:])
        if not self.navigation.track_obj:
            self.current_orientation = None, None, None

    def UpdateNavigationStatus(self, nav_status, vis_status):
        if not nav_status:
            self.nav_status = False
            self.current_orientation = None, None, None
        else:
            self.nav_status = True

    def UpdateSeedCoordinates(self, root=None, affine_vtk=None, coord_offset=(0, 0, 0), coord_offset_w=(0, 0, 0)):
        self.current_seed = coord_offset_w

    def OnMouseRightDown(self, evt):
        # TODO: Enable the "Set as target" only when target is created with registered object
        menu_id = wx.Menu()

        edit_id = menu_id.Append(0, _('Edit label'))
        menu_id.Bind(wx.EVT_MENU, self.OnMenuEditMarkerLabel, edit_id)

        color_id = menu_id.Append(1, _('Edit color'))
        menu_id.Bind(wx.EVT_MENU, self.OnMenuSetColor, color_id)

        menu_id.AppendSeparator()

        if self.__find_target_marker() == self.marker_list_ctrl.GetFocusedItem():
            target_menu = menu_id.Append(2, _('Remove target'))
            menu_id.Bind(wx.EVT_MENU, self.OnMenuRemoveTarget, target_menu)
            if has_mTMS:
                brain_target_menu = menu_id.Append(3, _('Set brain target'))
                menu_id.Bind(wx.EVT_MENU, self.OnSetBrainTarget, brain_target_menu)
        else:
            target_menu = menu_id.Append(2, _('Set as target'))
            menu_id.Bind(wx.EVT_MENU, self.OnMenuSetTarget, target_menu)

        orientation_menu = menu_id.Append(5, _('Set coil target orientation'))
        menu_id.Bind(wx.EVT_MENU, self.OnMenuSetCoilOrientation, orientation_menu)
        is_brain_target = self.markers[self.marker_list_ctrl.GetFocusedItem()].is_brain_target
        if is_brain_target and has_mTMS:
            send_brain_target_menu = menu_id.Append(6, _('Send brain target to mTMS'))
            menu_id.Bind(wx.EVT_MENU, self.OnSendBrainTarget, send_brain_target_menu)

        if self.nav_status and self.navigation.e_field_loaded:
            #Publisher.sendMessage('Check efield data')
            #if not tuple(np.argwhere(self.indexes_saved_lists == self.marker_list_ctrl.GetFocusedItem())):
            if self.__find_target_marker()  == self.marker_list_ctrl.GetFocusedItem():
                efield_menu = menu_id.Append(8, _('Save Efield target Data'))
                menu_id.Bind(wx.EVT_MENU, self.OnMenuSaveEfieldTargetData, efield_menu)

        if self.navigation.e_field_loaded:
            Publisher.sendMessage('Check efield data')
            if self.efield_data_saved:
                if tuple(np.argwhere(self.indexes_saved_lists==self.marker_list_ctrl.GetFocusedItem())):
                    if self.efield_target_idx  == self.marker_list_ctrl.GetFocusedItem():
                        efield_target_menu  = menu_id.Append(9, _('Remove Efield target'))
                        menu_id.Bind(wx.EVT_MENU, self.OnMenuRemoveEfieldTarget, efield_target_menu )
                    else:
                        efield_target_menu = menu_id.Append(9, _('Set as Efield target'))
                        menu_id.Bind(wx.EVT_MENU, self.OnMenuSetEfieldTarget, efield_target_menu)

        if self.navigation.e_field_loaded and not self.nav_status:
            if self.__find_target_marker() == self.marker_list_ctrl.GetFocusedItem():
                efield_vector_plot_menu = menu_id.Append(10,_('Show vector field'))
                menu_id.Bind(wx.EVT_MENU, self.OnMenuShowVectorField, efield_vector_plot_menu)


        menu_id.AppendSeparator()

        # Enable "Send target to robot" button only if tracker is robot, if navigation is on and if target is not none
        if self.tracker.tracker_id == const.ROBOT:
            send_target_to_robot = menu_id.Append(7, _('Send InVesalius target to robot'))
            menu_id.Bind(wx.EVT_MENU, self.OnMenuSendTargetToRobot, send_target_to_robot)

            send_target_to_robot.Enable(False)

            if self.nav_status and self.target_mode and (self.marker_list_ctrl.GetFocusedItem() == self.__find_target_marker()):
                send_target_to_robot.Enable(True)

        is_target_orientation_set = all([elem is not None for elem in self.markers[self.marker_list_ctrl.GetFocusedItem()].orientation])

        if is_target_orientation_set and not is_brain_target:
            target_menu.Enable(True)
        else:
            target_menu.Enable(False)

        self.PopupMenu(menu_id)
        menu_id.Destroy()

    def OnItemBlink(self, evt):
        Publisher.sendMessage('Blink Marker', index=self.marker_list_ctrl.GetFocusedItem())

    def OnStopItemBlink(self, evt):
        Publisher.sendMessage('Stop Blink Marker')

    def OnMenuEditMarkerLabel(self, evt):
        list_index = self.marker_list_ctrl.GetFocusedItem()
        if list_index == -1:
            wx.MessageBox(_("No data selected."), _("InVesalius 3"))
            return

        new_label = dlg.ShowEnterMarkerID(self.marker_list_ctrl.GetItemText(list_index, const.LABEL_COLUMN))
        self.markers[list_index].label = str(new_label)
        self.marker_list_ctrl.SetItem(list_index, const.LABEL_COLUMN, new_label)

        self.SaveState()

    def OnMenuSetTarget(self, evt):
        idx = self.marker_list_ctrl.GetFocusedItem()
        if idx == -1:
            wx.MessageBox(_("No data selected."), _("InVesalius 3"))
            return

        if self.tracker.tracker_id == const.ROBOT:
            Publisher.sendMessage('Update robot target', robot_tracker_flag=False,
                                  target_index=None, target=None)
        self.__set_marker_as_target(idx)

        self.SaveState()

    def GetEfieldDataStatus(self, efield_data_loaded, indexes_saved_list):
        self.indexes_saved_lists= []
        self.efield_data_saved = efield_data_loaded
        self.indexes_saved_lists = indexes_saved_list

    def OnMenuShowVectorField(self, evt):
        import invesalius.data.transformations as tr
        list_index = self.marker_list_ctrl.GetFocusedItem()
        position = self.markers[list_index].position
        orientation = np.radians(self.markers[list_index].orientation)
        Publisher.sendMessage('Calculate position and rotation', position=position, orientation=orientation)
        coord = [position, orientation]
        coord = np.array(coord).flatten()

        #Check here, it resets the radious list
        Publisher.sendMessage('Update interseccion offline', m_img =self.m_img_offline, coord = coord)

        enorm = self.navigation.neuronavigation_api.update_efield_vectorROI(position=self.cp,
                                                                  orientation=orientation,
                                                                  T_rot=self.T_rot,
                                                                  id_list=self.ID_list)
        enorm_data = [self.T_rot, self.cp, coord, enorm, self.ID_list]
        Publisher.sendMessage('Get enorm', enorm_data = enorm_data , plot_vector = True)

    def GetRotationPosition(self, T_rot, cp, m_img):
        self.T_rot = T_rot
        self.cp = cp
        self.m_img_offline = m_img

    def GetIdList(self, ID_list):
        self.ID_list = ID_list

    def OnMenuSetEfieldTarget(self,evt):
        idx = self.marker_list_ctrl.GetFocusedItem()
        if idx == -1:
            wx.MessageBox(_("No data selected."), _("InVesalius 3"))
            return
        self.__set_marker_as_target(idx)
        self.efield_target_idx = idx
        Publisher.sendMessage('Get target index efield', target_index_list = idx )

    def OnMenuSaveEfieldTargetData(self,evt):
        list_index = self.marker_list_ctrl.GetFocusedItem()
        position = self.markers[list_index].position
        orientation = self.markers[list_index].orientation
        Publisher.sendMessage('Save target data', target_list_index = list_index, position = position, orientation = orientation)

    def OnMenuSetCoilOrientation(self, evt):
        list_index = self.marker_list_ctrl.GetFocusedItem()
        position = self.markers[list_index].position
        orientation = self.markers[list_index].orientation

        dialog = dlg.SetCoilOrientationDialog(marker=position+orientation, brain_actor=self.brain_actor)
        if dialog.ShowModal() == wx.ID_OK:
            coil_position_list, coil_orientation_list, brain_position_list, brain_orientation_list = dialog.GetValue()
            self.CreateMarker(list(coil_position_list[0]), list(coil_orientation_list[0]), is_brain_target=False)
            for (position, orientation) in zip(brain_position_list, brain_orientation_list):
                self.CreateMarker(list(position), list(orientation), is_brain_target=True)
        dialog.Destroy()

        self.SaveState()

    def OnMenuRemoveEfieldTarget(self,evt):
        idx = self.marker_list_ctrl.GetFocusedItem()
        self.markers[idx].is_target = False
        self.marker_list_ctrl.SetItemBackgroundColour(idx, 'white')
        Publisher.sendMessage('Set target transparency', status=False, index=idx)
        self.marker_list_ctrl.SetItem(idx, const.TARGET_COLUMN, "")
        Publisher.sendMessage('Disable or enable coil tracker', status=False)
        Publisher.sendMessage('Update target', coord=None)
        self.efield_target_idx = None
        #self.__delete_all_brain_targets()
        wx.MessageBox(_("Efield target removed."), _("InVesalius 3"))

    def OnMenuRemoveTarget(self, evt):
        idx = self.marker_list_ctrl.GetFocusedItem()
        self.markers[idx].is_target = False
        self.marker_list_ctrl.SetItemBackgroundColour(idx, 'white')
        Publisher.sendMessage('Set target transparency', status=False, index=idx)
        self.marker_list_ctrl.SetItem(idx, const.TARGET_COLUMN, "")
        Publisher.sendMessage('Disable or enable coil tracker', status=False)
        Publisher.sendMessage('Update target', coord=None)
        if self.tracker.tracker_id == const.ROBOT:
            Publisher.sendMessage('Update robot target', robot_tracker_flag=False,
                                  target_index=None, target=None)
        #self.__delete_all_brain_targets()
        wx.MessageBox(_("Target removed."), _("InVesalius 3"))

        self.SaveState()

    def OnMenuSetColor(self, evt):
        index = self.marker_list_ctrl.GetFocusedItem()
        if index == -1:
            wx.MessageBox(_("No data selected."), _("InVesalius 3"))
            return

        color_current = [ch * 255 for ch in self.markers[index].colour]

        color_new = dlg.ShowColorDialog(color_current=color_current)

        if not color_new:
            return

        assert len(color_new) == 3

        # XXX: Seems like a slightly too early point for rounding; better to round only when the value
        #      is printed to the screen or file.
        #
        self.markers[index].colour = [round(s / 255.0, 3) for s in color_new]

        Publisher.sendMessage('Set new color', index=index, color=color_new)

        self.SaveState()

    def OnMenuSendTargetToRobot(self, evt):
        if isinstance(evt, int):
           self.marker_list_ctrl.Focus(evt)

        index = self.marker_list_ctrl.GetFocusedItem()
        if index == -1:
            wx.MessageBox(_("No data selected."), _("InVesalius 3"))
            return

        Publisher.sendMessage('Reset robot process', data=None)
        matrix_tracker_fiducials = self.tracker.GetMatrixTrackerFiducials()
        Publisher.sendMessage('Update tracker fiducials matrix',
                              matrix_tracker_fiducials=matrix_tracker_fiducials)

        nav_target = self.markers[index].position + self.markers[index].orientation
        coord_raw, markers_flag = self.tracker.TrackerCoordinates.GetCoordinates()
        m_target = dcr.image_to_tracker(self.navigation.m_change, coord_raw, nav_target, self.icp, self.navigation.obj_data)

        Publisher.sendMessage('Update robot target', robot_tracker_flag=True, target_index=self.marker_list_ctrl.GetFocusedItem(), target=m_target.tolist())

    def OnSetBrainTarget(self, evt):
        if isinstance(evt, int):
           self.marker_list_ctrl.Focus(evt)
        index = self.marker_list_ctrl.GetFocusedItem()
        if index == -1:
            wx.MessageBox(_("No data selected."), _("InVesalius 3"))
            return

        position = self.markers[index].position
        orientation = self.markers[index].orientation
        dialog = dlg.SetCoilOrientationDialog(mTMS=self.mTMS, marker=position+orientation, brain_target=True, brain_actor=self.brain_actor)

        if dialog.ShowModal() == wx.ID_OK:
            position_list, orientation_list = dialog.GetValueBrainTarget()
            for (position, orientation) in zip(position_list, orientation_list):
                self.CreateMarker(list(position), list(orientation), size=0.05, is_brain_target=True)
        dialog.Destroy()

        self.SaveState()

    def OnSendBrainTarget(self, evt):
        if isinstance(evt, int):
           self.marker_list_ctrl.Focus(evt)
        index = self.marker_list_ctrl.GetFocusedItem()
        if index == -1:
            wx.MessageBox(_("No data selected."), _("InVesalius 3"))
            return
        brain_target = self.markers[index].position + self.markers[index].orientation
        if self.__find_target_marker():
            coil_pose = self.markers[self.__find_target_marker()].position+self.markers[self.__find_target_marker()].orientation
            if self.navigation.coil_at_target:
                self.mTMS.UpdateTarget(coil_pose, brain_target)
                #wx.CallAfter(Publisher.sendMessage, 'Send brain target to mTMS API', coil_pose=coil_pose, brain_target=brain_target)
                print("Send brain target to mTMS API")
            else:
                print("The coil is not at the target")
        else:
            print("Target not set")

    def OnDeleteAllMarkers(self, evt=None):
        if evt is not None:
            result = dlg.ShowConfirmationDialog(msg=_("Remove all markers? Cannot be undone."))
            if result != wx.ID_OK:
                return

        if self.__find_target_marker() is not None:
            Publisher.sendMessage('Disable or enable coil tracker', status=False)
            if evt is not None:
                wx.MessageBox(_("Target deleted."), _("InVesalius 3"))
            if self.tracker.tracker_id == const.ROBOT:
                Publisher.sendMessage('Update robot target', robot_tracker_flag=False,
                                      target_index=None, target=None)

        self.markers = []
        Publisher.sendMessage('Remove all markers', indexes=self.marker_list_ctrl.GetItemCount())
        self.marker_list_ctrl.DeleteAllItems()
        Publisher.sendMessage('Stop Blink Marker', index='DeleteAll')

        self.SaveState()

    def OnDeleteMultipleMarkers(self, evt=None, label=None):
        # OnDeleteMultipleMarkers is used for both pubsub and button click events
        # Pubsub is used for fiducial handle and button click for all others

        if not evt:
            # Called through pubsub.

            indexes = []
            if label and (label in self.__list_fiducial_labels()):
                for id_n in range(self.marker_list_ctrl.GetItemCount()):
                    item = self.marker_list_ctrl.GetItem(id_n, const.LABEL_COLUMN)
                    if item.GetText() == label:
                        self.marker_list_ctrl.Focus(item.GetId())
                        indexes = [self.marker_list_ctrl.GetFocusedItem()]
        else:
            # Called using a button click.
            indexes = self.__get_selected_items()

        if not indexes:
            # Don't show the warning if called through pubsub
            if evt:
                wx.MessageBox(_("No data selected."), _("InVesalius 3"))
            return

        # If current target is removed, handle it as a special case.
        if self.__find_target_marker() in indexes:
            Publisher.sendMessage('Disable or enable coil tracker', status=False)
            Publisher.sendMessage('Update target', coord=None)
            if self.tracker.tracker_id == const.ROBOT:
                Publisher.sendMessage('Update robot target', robot_tracker_flag=False,
                                        target_index=None, target=None)
            wx.MessageBox(_("Target deleted."), _("InVesalius 3"))

        self.__delete_multiple_markers(indexes)
        self.SaveState()

    def OnCreateMarker(self, evt):
        self.CreateMarker()

        self.SaveState()

    def OnLoadMarkers(self, evt):
        """Loads markers from file and appends them to the current marker list.
        The file should contain no more than a single target marker. Also the
        file should not contain any fiducials already in the list."""
        filename = dlg.ShowLoadSaveDialog(message=_(u"Load markers"),
                                          wildcard=const.WILDCARD_MARKER_FILES)
                
        if not filename:
            return
        
        try:
            with open(filename, 'r') as file:
                magick_line = file.readline()
                assert magick_line.startswith(const.MARKER_FILE_MAGICK_STRING)
                ver = int(magick_line.split('_')[-1])
                if ver != 0:
                    wx.MessageBox(_("Unknown version of the markers file."), _("InVesalius 3"))
                    return
                
                file.readline() # skip the header line

                # Read the data lines and create markers
                for line in file.readlines():
                    marker = self.Marker()
                    marker.from_string(line)
                    self.CreateMarker(position=marker.position, orientation=marker.orientation, colour=marker.colour, size=marker.size,
                                      label=marker.label, is_target=False, seed=marker.seed, session_id=marker.session_id, is_brain_target=marker.is_brain_target)

                    if marker.label in self.__list_fiducial_labels():
                        Publisher.sendMessage('Load image fiducials', label=marker.label, position=marker.position)

                    # If the new marker has is_target=True, we first create
                    # a marker with is_target=False, and then call __set_marker_as_target
                    if marker.is_target:
                        self.__set_marker_as_target(len(self.markers) - 1)

        except Exception as e:
            wx.MessageBox(_("Invalid markers file."), _("InVesalius 3"))

        self.SaveState()

    def OnMarkersVisibility(self, evt, ctrl):
        if ctrl.GetValue():
            Publisher.sendMessage('Hide all markers',  indexes=self.marker_list_ctrl.GetItemCount())
            ctrl.SetLabel('Show')
        else:
            Publisher.sendMessage('Show all markers',  indexes=self.marker_list_ctrl.GetItemCount())
            ctrl.SetLabel('Hide')

    def OnSaveMarkers(self, evt):
        prj_data = prj.Project()
        timestamp = time.localtime(time.time())
        stamp_date = '{:0>4d}{:0>2d}{:0>2d}'.format(timestamp.tm_year, timestamp.tm_mon, timestamp.tm_mday)
        stamp_time = '{:0>2d}{:0>2d}{:0>2d}'.format(timestamp.tm_hour, timestamp.tm_min, timestamp.tm_sec)
        sep = '-'
        parts = [stamp_date, stamp_time, prj_data.name, 'markers']
        default_filename = sep.join(parts) + '.mkss'

        filename = dlg.ShowLoadSaveDialog(message=_(u"Save markers as..."),
                                          wildcard=const.WILDCARD_MARKER_FILES,
                                          style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
                                          default_filename=default_filename)

        if not filename:
            return

        try:
            with open(filename, 'w', newline='') as file:
                file.writelines(['%s%i\n' % (const.MARKER_FILE_MAGICK_STRING, const.CURRENT_MARKER_FILE_VERSION)])
                file.writelines(['%s\n' % self.Marker.to_string_headers()])
                file.writelines('%s\n' % marker.to_string() for marker in self.markers)
                file.close()
        except:
            wx.MessageBox(_("Error writing markers file."), _("InVesalius 3"))  

    def OnSelectColour(self, evt, ctrl):
        # TODO: Make sure GetValue returns 3 numbers (without alpha)
        self.marker_colour = [colour / 255.0 for colour in ctrl.GetValue()][:3]

    def OnSelectSize(self, evt, ctrl):
        self.marker_size = ctrl.GetValue()

    def OnChangeCurrentSession(self, new_session_id):
        self.current_session = new_session_id

    def UpdateMarkerOrientation(self, marker_id=None):
        list_index = marker_id if marker_id else 0
        position = self.markers[list_index].position
        orientation = self.markers[list_index].orientation
        dialog = dlg.SetCoilOrientationDialog(mTMS=self.mTMS, marker=position+orientation)

        if dialog.ShowModal() == wx.ID_OK:
            orientation = dialog.GetValue()
            Publisher.sendMessage('Update target orientation',
                                  target_id=marker_id, orientation=list(orientation))
        dialog.Destroy()

    def OnActivateTargetMode(self, evt=None, target_mode=None):
        self.target_mode = target_mode

    def AddPeeledSurface(self, flag, actor):
        self.brain_actor = actor

    def SetMarkers(self, markers):
        """
        Set all markers, overwriting the previous markers.
        """

        self.__delete_all_markers()

        for marker in markers:
            size = marker["size"]
            colour = marker["colour"]
            position = marker["position"]
            orientation = marker["orientation"]

            self.CreateMarker(
                size=size,
                colour=colour,
                position=position,
                orientation=orientation,
            )

        self.SaveState()


    def CreateMarker(self, position=None, orientation=None, colour=None, size=None, label='*', is_target=False, seed=None, session_id=None, is_brain_target=False):
        new_marker = self.Marker()
        new_marker.position = position or self.current_position
        new_marker.orientation = orientation or self.current_orientation
        new_marker.colour = colour or self.marker_colour
        new_marker.size = size or self.marker_size
        new_marker.label = label
        new_marker.is_target = is_target
        new_marker.seed = seed or self.current_seed
        new_marker.session_id = session_id or self.current_session
        new_marker.is_brain_target = is_brain_target

        if self.tracker.tracker_id == const.ROBOT and self.nav_status:
            current_head_robot_target_status = True
        else:
            current_head_robot_target_status = False

        if all([elem is not None for elem in new_marker.orientation]):
            arrow_flag = True
        else:
            arrow_flag = False

        if is_brain_target:
            new_marker.colour = [0, 0, 1]

        # Note that ball_id is zero-based, so we assign it len(self.markers) before the new marker is added
        marker_id = len(self.markers)

        Publisher.sendMessage('Add marker',
                              marker_id=marker_id,
                              size=new_marker.size,
                              colour=new_marker.colour,
                              position=new_marker.position,
                              orientation=new_marker.orientation,
                              arrow_flag=arrow_flag)

        self.markers.append(new_marker)

        # Add item to list control in panel
        num_items = self.marker_list_ctrl.GetItemCount()
        self.marker_list_ctrl.InsertItem(num_items, str(num_items + 1))
        if is_brain_target:
            self.marker_list_ctrl.SetItemBackgroundColour(num_items, wx.Colour(102, 178, 255))
        self.marker_list_ctrl.SetItem(num_items, const.SESSION_COLUMN, str(new_marker.session_id))
        self.marker_list_ctrl.SetItem(num_items, const.LABEL_COLUMN, new_marker.label)

        if self.session.GetConfig('debug'):
            self.marker_list_ctrl.SetItem(num_items, const.X_COLUMN, str(round(new_marker.x, 1)))
            self.marker_list_ctrl.SetItem(num_items, const.Y_COLUMN, str(round(new_marker.y, 1)))
            self.marker_list_ctrl.SetItem(num_items, const.Z_COLUMN, str(round(new_marker.z, 1)))

        self.marker_list_ctrl.EnsureVisible(num_items)


class DbsPanel(wx.Panel):
    def __init__(self, parent):
        wx.Panel.__init__(self, parent)
        try:
            default_colour = wx.SystemSettings.GetColour(wx.SYS_COLOUR_MENUBAR)
        except AttributeError:
            default_colour = wx.SystemSettings_GetColour(wx.SYS_COLOUR_MENUBAR)


class TractographyPanel(wx.Panel):

    def __init__(self, parent):
        wx.Panel.__init__(self, parent)
        try:
            default_colour = wx.SystemSettings.GetColour(wx.SYS_COLOUR_MENUBAR)
        except AttributeError:
            default_colour = wx.SystemSettings_GetColour(wx.SYS_COLOUR_MENUBAR)
        self.SetBackgroundColour(default_colour)

        self.affine = np.identity(4)
        self.affine_vtk = None
        self.trekker = None
        self.n_tracts = const.N_TRACTS
        self.peel_depth = const.PEEL_DEPTH
        self.view_tracts = False
        self.seed_offset = const.SEED_OFFSET
        self.seed_radius = const.SEED_RADIUS
        self.sleep_nav = const.SLEEP_NAVIGATION
        self.brain_opacity = const.BRAIN_OPACITY
        self.brain_peel = None
        self.brain_actor = None
        self.n_peels = const.MAX_PEEL_DEPTH
        self.p_old = np.array([[0., 0., 0.]])
        self.tracts_run = None
        self.trekker_cfg = const.TREKKER_CONFIG
        self.nav_status = False
        self.peel_loaded = False
        self.SetAutoLayout(1)
        self.__bind_events()

        # Button for import config coil file
        tooltip = wx.ToolTip(_("Load FOD"))
        btn_load = wx.Button(self, -1, _("FOD"), size=wx.Size(50, 23))
        btn_load.SetToolTip(tooltip)
        btn_load.Enable(1)
        btn_load.Bind(wx.EVT_BUTTON, self.OnLinkFOD)
        # self.btn_load = btn_load

        # Save button for object registration
        tooltip = wx.ToolTip(_(u"Load Trekker configuration parameters"))
        btn_load_cfg = wx.Button(self, -1, _(u"Configure"), size=wx.Size(65, 23))
        btn_load_cfg.SetToolTip(tooltip)
        btn_load_cfg.Enable(1)
        btn_load_cfg.Bind(wx.EVT_BUTTON, self.OnLoadParameters)
        # self.btn_load_cfg = btn_load_cfg

        # Button for creating new coil
        tooltip = wx.ToolTip(_("Load brain visualization"))
        btn_mask = wx.Button(self, -1, _("Brain"), size=wx.Size(50, 23))
        btn_mask.SetToolTip(tooltip)
        btn_mask.Enable(1)
        btn_mask.Bind(wx.EVT_BUTTON, self.OnLinkBrain)
        # self.btn_new = btn_new

        # Button for creating new coil
        tooltip = wx.ToolTip(_("Load anatomical labels"))
        btn_act = wx.Button(self, -1, _("ACT"), size=wx.Size(50, 23))
        btn_act.SetToolTip(tooltip)
        btn_act.Enable(1)
        btn_act.Bind(wx.EVT_BUTTON, self.OnLoadACT)
        # self.btn_new = btn_new

        # Create a horizontal sizer to represent button save
        line_btns = wx.BoxSizer(wx.HORIZONTAL)
        line_btns.Add(btn_load, 1, wx.LEFT | wx.TOP | wx.RIGHT, 2)
        line_btns.Add(btn_load_cfg, 1, wx.LEFT | wx.TOP | wx.RIGHT, 2)
        line_btns.Add(btn_mask, 1, wx.LEFT | wx.TOP | wx.RIGHT, 2)
        line_btns.Add(btn_act, 1, wx.LEFT | wx.TOP | wx.RIGHT, 2)

        # Change peeling depth
        text_peel_depth = wx.StaticText(self, -1, _("Peeling depth (mm):"))
        spin_peel_depth = wx.SpinCtrl(self, -1, "", size=wx.Size(50, 23))
        spin_peel_depth.Enable(1)
        spin_peel_depth.SetRange(0, const.MAX_PEEL_DEPTH)
        spin_peel_depth.SetValue(const.PEEL_DEPTH)
        spin_peel_depth.Bind(wx.EVT_TEXT, partial(self.OnSelectPeelingDepth, ctrl=spin_peel_depth))
        spin_peel_depth.Bind(wx.EVT_SPINCTRL, partial(self.OnSelectPeelingDepth, ctrl=spin_peel_depth))

        # Change number of tracts
        text_ntracts = wx.StaticText(self, -1, _("Number tracts:"))
        spin_ntracts = wx.SpinCtrl(self, -1, "", size=wx.Size(50, 23))
        spin_ntracts.Enable(1)
        spin_ntracts.SetRange(1, 2000)
        spin_ntracts.SetValue(const.N_TRACTS)
        spin_ntracts.Bind(wx.EVT_TEXT, partial(self.OnSelectNumTracts, ctrl=spin_ntracts))
        spin_ntracts.Bind(wx.EVT_SPINCTRL, partial(self.OnSelectNumTracts, ctrl=spin_ntracts))

        # Change seed offset for computing tracts
        text_offset = wx.StaticText(self, -1, _("Seed offset (mm):"))
        spin_offset = wx.SpinCtrlDouble(self, -1, "", size=wx.Size(50, 23), inc = 0.1)
        spin_offset.Enable(1)
        spin_offset.SetRange(0, 100.0)
        spin_offset.SetValue(self.seed_offset)
        spin_offset.Bind(wx.EVT_TEXT, partial(self.OnSelectOffset, ctrl=spin_offset))
        spin_offset.Bind(wx.EVT_SPINCTRL, partial(self.OnSelectOffset, ctrl=spin_offset))
        # self.spin_offset = spin_offset

        # Change seed radius for computing tracts
        text_radius = wx.StaticText(self, -1, _("Seed radius (mm):"))
        spin_radius = wx.SpinCtrlDouble(self, -1, "", size=wx.Size(50, 23), inc=0.1)
        spin_radius.Enable(1)
        spin_radius.SetRange(0, 100.0)
        spin_radius.SetValue(self.seed_radius)
        spin_radius.Bind(wx.EVT_TEXT, partial(self.OnSelectRadius, ctrl=spin_radius))
        spin_radius.Bind(wx.EVT_SPINCTRL, partial(self.OnSelectRadius, ctrl=spin_radius))
        # self.spin_radius = spin_radius

        # Change sleep pause between navigation loops
        text_sleep = wx.StaticText(self, -1, _("Sleep (s):"))
        spin_sleep = wx.SpinCtrlDouble(self, -1, "", size=wx.Size(50, 23), inc=0.01)
        spin_sleep.Enable(1)
        spin_sleep.SetRange(0.01, 10.0)
        spin_sleep.SetValue(self.sleep_nav)
        spin_sleep.Bind(wx.EVT_TEXT, partial(self.OnSelectSleep, ctrl=spin_sleep))
        spin_sleep.Bind(wx.EVT_SPINCTRL, partial(self.OnSelectSleep, ctrl=spin_sleep))

        # Change opacity of brain mask visualization
        text_opacity = wx.StaticText(self, -1, _("Brain opacity:"))
        spin_opacity = wx.SpinCtrlDouble(self, -1, "", size=wx.Size(50, 23), inc=0.1)
        spin_opacity.Enable(0)
        spin_opacity.SetRange(0, 1.0)
        spin_opacity.SetValue(self.brain_opacity)
        spin_opacity.Bind(wx.EVT_TEXT, partial(self.OnSelectOpacity, ctrl=spin_opacity))
        spin_opacity.Bind(wx.EVT_SPINCTRL, partial(self.OnSelectOpacity, ctrl=spin_opacity))
        self.spin_opacity = spin_opacity

        # Create a horizontal sizer to threshold configs
        border = 1
        line_peel_depth = wx.BoxSizer(wx.HORIZONTAL)
        line_peel_depth.AddMany([(text_peel_depth, 1, wx.EXPAND | wx.GROW | wx.TOP | wx.RIGHT | wx.LEFT, border),
                                 (spin_peel_depth, 0, wx.ALL | wx.EXPAND | wx.GROW, border)])

        line_ntracts = wx.BoxSizer(wx.HORIZONTAL)
        line_ntracts.AddMany([(text_ntracts, 1, wx.EXPAND | wx.GROW | wx.TOP | wx.RIGHT | wx.LEFT, border),
                              (spin_ntracts, 0, wx.ALL | wx.EXPAND | wx.GROW, border)])

        line_offset = wx.BoxSizer(wx.HORIZONTAL)
        line_offset.AddMany([(text_offset, 1, wx.EXPAND | wx.GROW | wx.TOP | wx.RIGHT | wx.LEFT, border),
                             (spin_offset, 0, wx.ALL | wx.EXPAND | wx.GROW, border)])

        line_radius = wx.BoxSizer(wx.HORIZONTAL)
        line_radius.AddMany([(text_radius, 1, wx.EXPAND | wx.GROW | wx.TOP | wx.RIGHT | wx.LEFT, border),
                             (spin_radius, 0, wx.ALL | wx.EXPAND | wx.GROW, border)])

        line_sleep = wx.BoxSizer(wx.HORIZONTAL)
        line_sleep.AddMany([(text_sleep, 1, wx.EXPAND | wx.GROW | wx.TOP | wx.RIGHT | wx.LEFT, border),
                            (spin_sleep, 0, wx.ALL | wx.EXPAND | wx.GROW, border)])

        line_opacity = wx.BoxSizer(wx.HORIZONTAL)
        line_opacity.AddMany([(text_opacity, 1, wx.EXPAND | wx.GROW | wx.TOP | wx.RIGHT | wx.LEFT, border),
                            (spin_opacity, 0, wx.ALL | wx.EXPAND | wx.GROW, border)])

        # Check box to enable tract visualization
        checktracts = wx.CheckBox(self, -1, _('Enable tracts'))
        checktracts.SetValue(False)
        checktracts.Enable(0)
        checktracts.Bind(wx.EVT_CHECKBOX, partial(self.OnEnableTracts, ctrl=checktracts))
        self.checktracts = checktracts

        # Check box to enable surface peeling
        checkpeeling = wx.CheckBox(self, -1, _('Peel surface'))
        checkpeeling.SetValue(False)
        checkpeeling.Enable(0)
        checkpeeling.Bind(wx.EVT_CHECKBOX, partial(self.OnShowPeeling, ctrl=checkpeeling))
        self.checkpeeling = checkpeeling

        # Check box to enable tract visualization
        checkACT = wx.CheckBox(self, -1, _('ACT'))
        checkACT.SetValue(False)
        checkACT.Enable(0)
        checkACT.Bind(wx.EVT_CHECKBOX, partial(self.OnEnableACT, ctrl=checkACT))
        self.checkACT = checkACT

        border_last = 1
        line_checks = wx.BoxSizer(wx.HORIZONTAL)
        line_checks.Add(checktracts, 0, wx.ALIGN_LEFT | wx.RIGHT | wx.LEFT, border_last)
        line_checks.Add(checkpeeling, 0, wx.ALIGN_CENTER | wx.RIGHT | wx.LEFT, border_last)
        line_checks.Add(checkACT, 0, wx.RIGHT | wx.LEFT, border_last)

        # Add line sizers into main sizer
        border = 1
        border_last = 10
        main_sizer = wx.BoxSizer(wx.VERTICAL)
        main_sizer.Add(line_btns, 0, wx.BOTTOM | wx.ALIGN_CENTER_HORIZONTAL, border_last)
        main_sizer.Add(line_peel_depth, 0, wx.GROW | wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, border)
        main_sizer.Add(line_ntracts, 0, wx.GROW | wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, border)
        main_sizer.Add(line_offset, 0, wx.GROW | wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, border)
        main_sizer.Add(line_radius, 0, wx.GROW | wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, border)
        main_sizer.Add(line_sleep, 0, wx.GROW | wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, border)
        main_sizer.Add(line_opacity, 0, wx.GROW | wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, border)
        main_sizer.Add(line_checks, 0, wx.GROW | wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP | wx.BOTTOM, border_last)
        main_sizer.Fit(self)

        self.SetSizer(main_sizer)
        self.Update()

    def __bind_events(self):
        Publisher.subscribe(self.OnCloseProject, 'Close project data')
        Publisher.subscribe(self.OnUpdateTracts, 'Set cross focal point')
        Publisher.subscribe(self.UpdateNavigationStatus, 'Navigation status')

    def OnSelectPeelingDepth(self, evt, ctrl):
        self.peel_depth = ctrl.GetValue()
        if self.checkpeeling.GetValue():
            actor = self.brain_peel.get_actor(self.peel_depth)
            Publisher.sendMessage('Update peel', flag=True, actor=actor)
            Publisher.sendMessage('Get peel centers and normals', centers=self.brain_peel.peel_centers,
                                  normals=self.brain_peel.peel_normals)
            Publisher.sendMessage('Get init locator', locator=self.brain_peel.locator)
            self.peel_loaded = True
    def OnSelectNumTracts(self, evt, ctrl):
        self.n_tracts = ctrl.GetValue()
        # self.tract.n_tracts = ctrl.GetValue()
        Publisher.sendMessage('Update number of tracts', data=self.n_tracts)

    def OnSelectOffset(self, evt, ctrl):
        self.seed_offset = ctrl.GetValue()
        # self.tract.seed_offset = ctrl.GetValue()
        Publisher.sendMessage('Update seed offset', data=self.seed_offset)

    def OnSelectRadius(self, evt, ctrl):
        self.seed_radius = ctrl.GetValue()
        # self.tract.seed_offset = ctrl.GetValue()
        Publisher.sendMessage('Update seed radius', data=self.seed_radius)

    def OnSelectSleep(self, evt, ctrl):
        self.sleep_nav = ctrl.GetValue()
        # self.tract.seed_offset = ctrl.GetValue()
        Publisher.sendMessage('Update sleep', data=self.sleep_nav)

    def OnSelectOpacity(self, evt, ctrl):
        self.brain_actor.GetProperty().SetOpacity(ctrl.GetValue())
        Publisher.sendMessage('Update peel', flag=True, actor=self.brain_actor)

    def OnShowPeeling(self, evt, ctrl):
        # self.view_peeling = ctrl.GetValue()
        if ctrl.GetValue():
            actor = self.brain_peel.get_actor(self.peel_depth)
            self.peel_loaded = True
            Publisher.sendMessage('Update peel visualization', data=self.peel_loaded)
        else:
            actor = None
            self.peel_loaded = False
            Publisher.sendMessage('Update peel visualization', data= self.peel_loaded)

        Publisher.sendMessage('Update peel', flag=ctrl.GetValue(), actor=actor)

    def OnEnableTracts(self, evt, ctrl):
        self.view_tracts = ctrl.GetValue()
        Publisher.sendMessage('Update tracts visualization', data=self.view_tracts)
        if not self.view_tracts:
            Publisher.sendMessage('Remove tracts')
            Publisher.sendMessage("Update marker offset state", create=False)

    def OnEnableACT(self, evt, ctrl):
        # self.view_peeling = ctrl.GetValue()
        # if ctrl.GetValue():
        #     act_data = self.brain_peel.get_actor(self.peel_depth)
        # else:
        #     actor = None
        Publisher.sendMessage('Enable ACT', data=ctrl.GetValue())

    def UpdateNavigationStatus(self, nav_status, vis_status):
        self.nav_status = nav_status

    def OnLinkBrain(self, event=None):
        Publisher.sendMessage('Begin busy cursor')
        inv_proj = prj.Project()
        peels_dlg = dlg.PeelsCreationDlg(wx.GetApp().GetTopWindow())
        ret = peels_dlg.ShowModal()
        method = peels_dlg.method
        if ret == wx.ID_OK:
            slic = sl.Slice()
            ww = slic.window_width
            wl = slic.window_level
            affine = np.eye(4)
            if method == peels_dlg.FROM_FILES:
                try:
                    affine = slic.affine.copy()
                except AttributeError:
                    pass

            self.brain_peel = brain.Brain(self.n_peels, ww, wl, affine, inv_proj)
            if method == peels_dlg.FROM_MASK:
                choices = [i for i in inv_proj.mask_dict.values()]
                mask_index = peels_dlg.cb_masks.GetSelection()
                mask = choices[mask_index]
                self.brain_peel.from_mask(mask)
            else:
                mask_path = peels_dlg.mask_path
                self.brain_peel.from_mask_file(mask_path)
            self.brain_actor = self.brain_peel.get_actor(self.peel_depth)
            self.brain_actor.GetProperty().SetOpacity(self.brain_opacity)
            Publisher.sendMessage('Update peel', flag=True, actor=self.brain_actor)
            Publisher.sendMessage('Get peel centers and normals', centers=self.brain_peel.peel_centers,
                                  normals=self.brain_peel.peel_normals)
            Publisher.sendMessage('Get init locator', locator=self.brain_peel.locator)
            self.checkpeeling.Enable(1)
            self.checkpeeling.SetValue(True)
            self.spin_opacity.Enable(1)
            Publisher.sendMessage('Update status text in GUI', label=_("Brain model loaded"))
            self.peel_loaded = True
            Publisher.sendMessage('Update peel visualization', data= self.peel_loaded)

        peels_dlg.Destroy()
        Publisher.sendMessage('End busy cursor')

    def OnLinkFOD(self, event=None):
        Publisher.sendMessage('Begin busy cursor')
        filename = dlg.ShowImportOtherFilesDialog(const.ID_NIFTI_IMPORT, msg=_("Import Trekker FOD"))
        # Juuso
        # data_dir = os.environ.get('OneDriveConsumer') + '\\data\\dti'
        # FOD_path = 'sub-P0_dwi_FOD.nii'
        # Baran
        # data_dir = os.environ.get('OneDrive') + r'\data\dti_navigation\baran\anat_reg_improve_20200609'
        # FOD_path = 'Baran_FOD.nii'
        # filename = os.path.join(data_dir, FOD_path)

        if not self.affine_vtk:
            slic = sl.Slice()
            prj_data = prj.Project()
            matrix_shape = tuple(prj_data.matrix_shape)
            spacing = tuple(prj_data.spacing)
            img_shift = spacing[1] * (matrix_shape[1] - 1)
            self.affine = slic.affine.copy()
            self.affine[1, -1] -= img_shift
            self.affine_vtk = vtk_utils.numpy_to_vtkMatrix4x4(self.affine)

        if filename:
            Publisher.sendMessage('Update status text in GUI', label=_("Busy"))
            try:
                self.trekker = Trekker.initialize(filename.encode('utf-8'))
                self.trekker, n_threads = dti.set_trekker_parameters(self.trekker, self.trekker_cfg)

                self.checktracts.Enable(1)
                self.checktracts.SetValue(True)
                self.view_tracts = True

                Publisher.sendMessage('Update Trekker object', data=self.trekker)
                Publisher.sendMessage('Update number of threads', data=n_threads)
                Publisher.sendMessage('Update tracts visualization', data=1)
                Publisher.sendMessage('Update status text in GUI', label=_("Trekker initialized"))
                # except:
                #     wx.MessageBox(_("Unable to initialize Trekker, check FOD and config files."), _("InVesalius 3"))
            except:
                Publisher.sendMessage('Update status text in GUI', label=_("Trekker initialization failed."))
                wx.MessageBox(_("Unable to load FOD."), _("InVesalius 3"))

        Publisher.sendMessage('End busy cursor')

    def OnLoadACT(self, event=None):
        if self.trekker:
            Publisher.sendMessage('Begin busy cursor')
            filename = dlg.ShowImportOtherFilesDialog(const.ID_NIFTI_IMPORT, msg=_("Import anatomical labels"))
            # Baran
            # data_dir = os.environ.get('OneDrive') + r'\data\dti_navigation\baran\anat_reg_improve_20200609'
            # act_path = 'Baran_trekkerACTlabels_inFODspace.nii'
            # filename = os.path.join(data_dir, act_path)

            if not self.affine_vtk:
                slic = sl.Slice()
                prj_data = prj.Project()
                matrix_shape = tuple(prj_data.matrix_shape)
                spacing = tuple(prj_data.spacing)
                img_shift = spacing[1] * (matrix_shape[1] - 1)
                self.affine = slic.affine.copy()
                self.affine[1, -1] -= img_shift
                self.affine_vtk = vtk_utils.numpy_to_vtkMatrix4x4(self.affine)

            try:
                Publisher.sendMessage('Update status text in GUI', label=_("Busy"))
                if filename:
                    act_data = nb.squeeze_image(nb.load(filename))
                    act_data = nb.as_closest_canonical(act_data)
                    act_data.update_header()
                    act_data_arr = act_data.get_fdata()

                    self.checkACT.Enable(1)
                    self.checkACT.SetValue(True)

                    # ACT rules should be as follows:
                    self.trekker.pathway_stop_at_entry(filename.encode('utf-8'), -1)  # outside
                    self.trekker.pathway_discard_if_ends_inside(filename.encode('utf-8'), 1)  # wm
                    self.trekker.pathway_discard_if_enters(filename.encode('utf-8'), 0)  # csf

                    Publisher.sendMessage('Update ACT data', data=act_data_arr)
                    Publisher.sendMessage('Enable ACT', data=True)
                    Publisher.sendMessage('Update status text in GUI', label=_("Trekker ACT loaded"))
            except:
                Publisher.sendMessage('Update status text in GUI', label=_("ACT initialization failed."))
                wx.MessageBox(_("Unable to load ACT."), _("InVesalius 3"))

            Publisher.sendMessage('End busy cursor')
        else:
            wx.MessageBox(_("Load FOD image before the ACT."), _("InVesalius 3"))

    def OnLoadParameters(self, event=None):
        import json
        filename = dlg.ShowLoadSaveDialog(message=_(u"Load Trekker configuration"),
                                          wildcard=_("JSON file (*.json)|*.json"))
        try:
            # Check if filename exists, read the JSON file and check if all parameters match
            # with the required list defined in the constants module
            # if a parameter is missing, raise an error
            if filename:
                with open(filename) as json_file:
                    self.trekker_cfg = json.load(json_file)
                assert all(name in self.trekker_cfg for name in const.TREKKER_CONFIG)
                if self.trekker:
                    self.trekker, n_threads = dti.set_trekker_parameters(self.trekker, self.trekker_cfg)
                    Publisher.sendMessage('Update Trekker object', data=self.trekker)
                    Publisher.sendMessage('Update number of threads', data=n_threads)

                Publisher.sendMessage('Update status text in GUI', label=_("Trekker config loaded"))

        except (AssertionError, json.decoder.JSONDecodeError):
            # Inform user that file is not compatible
            self.trekker_cfg = const.TREKKER_CONFIG
            wx.MessageBox(_("File incompatible, using default configuration."), _("InVesalius 3"))
            Publisher.sendMessage('Update status text in GUI', label="")

    def OnUpdateTracts(self, position):
        """
        Minimal working version of tract computation. Updates when cross sends Pubsub message to update.
        Position refers to the coordinates in InVesalius 2D space. To represent the same coordinates in the 3D space,
        flip_x the coordinates and multiply the z coordinate by -1. This is all done in the flix_x function.

        :param arg: event for pubsub
        :param position: list or array with the x, y, and z coordinates in InVesalius space
        """
        # Minimal working version of tract computation
        # It updates when cross updates
        # pass
        if self.view_tracts and not self.nav_status:
            # print("Running during navigation")
            coord_flip = list(position[:3])
            coord_flip[1] = -coord_flip[1]
            dti.compute_and_visualize_tracts(self.trekker, coord_flip, self.affine, self.affine_vtk,
                                             self.n_tracts)

    def OnCloseProject(self):
        self.trekker = None
        self.trekker_cfg = const.TREKKER_CONFIG

        self.checktracts.SetValue(False)
        self.checktracts.Enable(0)
        self.checkpeeling.SetValue(False)
        self.checkpeeling.Enable(0)
        self.checkACT.SetValue(False)
        self.checkACT.Enable(0)

        self.spin_opacity.SetValue(const.BRAIN_OPACITY)
        self.spin_opacity.Enable(0)
        Publisher.sendMessage('Update peel', flag=False, actor=self.brain_actor)

        self.peel_depth = const.PEEL_DEPTH
        self.n_tracts = const.N_TRACTS

        Publisher.sendMessage('Remove tracts')

class E_fieldPanel(wx.Panel):
    def __init__(self, parent, navigation):
        wx.Panel.__init__(self, parent)
        try:
            default_colour = wx.SystemSettings.GetColour(wx.SYS_COLOUR_MENUBAR)
        except AttributeError:
            default_colour = wx.SystemSettings_GetColour(wx.SYS_COLOUR_MENUBAR)
        self.__bind_events()

        self.SetBackgroundColour(default_colour)
        self.e_field_loaded = False
        self.e_field_brain = None
        self.e_field_mesh = None
        self.cortex_file = None
        self.meshes_file = None
        self.multilocus_coil = None
        self.coil = None
        self.ci = None
        self.co = None
        self.sleep_nav = const.SLEEP_NAVIGATION
        self.navigation = navigation
        self.session = ses.Session()
        #  Check box to enable e-field visualization
        enable_efield = wx.CheckBox(self, -1, _('Enable E-field'))
        enable_efield.SetValue(False)
        enable_efield.Enable(1)
        enable_efield.Bind(wx.EVT_CHECKBOX, partial(self.OnEnableEfield, ctrl=enable_efield))
        self.enable_efield = enable_efield

        plot_vectors = wx.CheckBox(self, -1, _('Plot Efield vectors'))
        plot_vectors.SetValue(False)
        plot_vectors.Enable(1)
        plot_vectors.Bind(wx.EVT_CHECKBOX, partial(self.OnEnablePlotVectors, ctrl=plot_vectors))

        tooltip2 = wx.ToolTip(_("Load Brain Json config"))
        btn_act2 = wx.Button(self, -1, _("Load Config"), size=wx.Size(100, 23))
        btn_act2.SetToolTip(tooltip2)
        btn_act2.Enable(1)
        btn_act2.Bind(wx.EVT_BUTTON, self.OnAddConfig)

        tooltip = wx.ToolTip(_("Save Efield"))
        self.btn_save = wx.Button(self, -1, _("Save Efield"), size=wx.Size(80, -1))
        self.btn_save.SetToolTip(tooltip)
        self.btn_save.Bind(wx.EVT_BUTTON, self.OnSaveEfield)
        self.btn_save.Enable(False)

        tooltip3 = wx.ToolTip(_("Save All Efield"))
        self.btn_all_save = wx.Button(self, -1, _("Save All Efield"), size=wx.Size(80, -1))
        self.btn_all_save.SetToolTip(tooltip3)
        self.btn_all_save.Bind(wx.EVT_BUTTON, self.OnSaveAllDataEfield)
        self.btn_all_save.Enable(False)

        text_sleep = wx.StaticText(self, -1, _("Sleep (s):"))
        spin_sleep = wx.SpinCtrlDouble(self, -1, "", size = wx.Size(50,23), inc = 0.01)
        spin_sleep.Enable(1)
        spin_sleep.SetRange(0.05,10.0)
        spin_sleep.SetValue(self.sleep_nav)
        spin_sleep.Bind(wx.EVT_TEXT, partial(self.OnSelectSleep, ctrl=spin_sleep))
        spin_sleep.Bind(wx.EVT_SPINCTRL, partial(self.OnSelectSleep, ctrl=spin_sleep))

        border = 1
        line_sleep = wx.BoxSizer(wx.VERTICAL)
        line_sleep.AddMany([(text_sleep, 1, wx.GROW | wx.TOP | wx.RIGHT | wx.LEFT, border),
                            (spin_sleep, 0, wx.ALL | wx.EXPAND | wx.GROW, border)])
        line_btns = wx.BoxSizer(wx.HORIZONTAL)
        line_btns.Add(btn_act2, 1, wx.LEFT | wx.TOP | wx.RIGHT, 2)

        line_btns_save = wx.BoxSizer(wx.HORIZONTAL)
        line_btns_save.Add(self.btn_save, 1, wx.LEFT | wx.TOP | wx.RIGHT, 2)
        line_btns_save.Add(self.btn_all_save, 1, wx.LEFT | wx.TOP | wx.RIGHT, 2)

        # Add line sizers into main sizer
        border_last = 5
        txt_surface = wx.StaticText(self, -1, _('Change coil:'), pos=(20,100))
        self.combo_surface_name = wx.ComboBox(self, -1, size=(100, 23), pos=(25, 20),
                                              style=wx.CB_DROPDOWN | wx.CB_READONLY)
        # combo_surface_name.SetSelection(0)
        self.combo_surface_name.Bind(wx.EVT_COMBOBOX_DROPDOWN, self.OnComboCoilNameClic)
        self.combo_surface_name.Bind(wx.EVT_COMBOBOX, self.OnComboCoil)
        self.combo_surface_name.Insert('Select coil:',0)
        self.combo_surface_name.Enable(False)

        main_sizer = wx.BoxSizer(wx.VERTICAL)
        main_sizer.Add(line_btns, 0, wx.BOTTOM | wx.ALIGN_CENTER_HORIZONTAL, border_last)
        main_sizer.Add(enable_efield, 1, wx.LEFT | wx.RIGHT, 2)
        main_sizer.Add(plot_vectors, 1, wx.LEFT | wx.RIGHT, 2)
        main_sizer.Add(self.combo_surface_name, 1, wx.ALIGN_CENTER_HORIZONTAL,2)
        main_sizer.Add(line_sleep, 0, wx.LEFT | wx.RIGHT | wx.TOP, border)
        main_sizer.Add(line_btns_save, 0, wx.BOTTOM | wx.ALIGN_CENTER_HORIZONTAL, border_last)

        main_sizer.SetSizeHints(self)
        self.SetSizer(main_sizer)

    def __bind_events(self):
        Publisher.subscribe(self.UpdateNavigationStatus, 'Navigation status')
        Publisher.subscribe(self.OnGetEfieldActor, 'Get Efield actor from json')
        Publisher.subscribe(self.OnGetEfieldPaths, 'Get Efield paths')
        Publisher.subscribe(self.OnGetMultilocusCoils,'Get multilocus paths from json')
        Publisher.subscribe(self.SendNeuronavigationApi, 'Send Neuronavigation Api')
        Publisher.subscribe(self.GetEfieldDataStatus, 'Get status of Efield saved data')

    def OnAddConfig(self, evt):
        filename = dlg.LoadConfigEfield()
        if filename:
            convert_to_inv = dlg.ImportMeshCoordSystem()
            Publisher.sendMessage('Update status in GUI', value=50, label="Loading E-field...")
            Publisher.sendMessage('Update convert_to_inv flag', convert_to_inv=convert_to_inv)
            Publisher.sendMessage('Read json config file for efield', filename=filename, convert_to_inv=convert_to_inv)
            self.Init_efield()

    def Init_efield(self):
        self.navigation.neuronavigation_api.initialize_efield(
            cortex_model_path=self.cortex_file,
            mesh_models_paths=self.meshes_file,
            coil_model_path=self.coil,
            conductivities_inside=self.ci,
            conductivities_outside=self.co,
        )
        Publisher.sendMessage('Update status in GUI', value=0, label="Ready")

    def OnEnableEfield(self, evt, ctrl):
        efield_enabled = ctrl.GetValue()
        if efield_enabled:
            if self.session.GetConfig('debug_efield'):
                debug_efield_enorm = dlg.ShowLoadCSVDebugEfield()
                if isinstance(debug_efield_enorm, np.ndarray):
                    self.navigation.debug_efield_enorm = debug_efield_enorm
                else:
                    dlg.Efield_debug_Enorm_warning()
                    self.enable_efield.SetValue(False)
                    self.e_field_loaded = False
                    self.navigation.e_field_loaded = self.e_field_loaded
                    return
            else:
                if not self.navigation.neuronavigation_api.connection:
                    dlg.Efield_connection_warning()
                    #self.combo_surface_name.Enable(False)
                    self.enable_efield.Enable(False)
                    self.e_field_loaded = False
                    return
            self.e_field_brain = brain.E_field_brain(self.e_field_mesh)
            Publisher.sendMessage('Initialize E-field brain', e_field_brain=self.e_field_brain)

            Publisher.sendMessage('Initialize color array')
            self.e_field_loaded = True
            self.combo_surface_name.Enable(True)
            self.btn_all_save.Enable(True)

        else:
            Publisher.sendMessage('Recolor again')
            self.e_field_loaded = False
            #self.combo_surface_name.Enable(True)
        self.navigation.e_field_loaded = self.e_field_loaded

    def OnEnablePlotVectors(self, evt, ctrl):
        self.plot_efield_vectors = ctrl.GetValue()
        self.navigation.plot_efield_vectors = self.plot_efield_vectors

    def OnComboNameClic(self, evt):
        import invesalius.project as prj
        proj = prj.Project()
        self.combo_surface_name.Clear()
        for n in range(len(proj.surface_dict)):
            self.combo_surface_name.Insert(str(proj.surface_dict[n].name), n)

    def OnComboCoilNameClic(self, evt):
        self.combo_surface_name.Clear()
        if self.multilocus_coil is not None:
            for elements in range(len(self.multilocus_coil)):
                self.combo_surface_name.Insert(self.multilocus_coil[elements], elements)

    def OnComboCoil(self, evt):
        coil_name = evt.GetString()
        coil_index = evt.GetSelection()
        self.OnChangeCoil(self.multilocus_coil[coil_index])
        #self.e_field_mesh = self.proj.surface_dict[self.surface_index].polydata
        #Publisher.sendMessage('Get Actor', surface_index = self.surface_index)

    def OnChangeCoil(self, coil_model_path):
        self.navigation.neuronavigation_api.efield_coil(
            coil_model_path=coil_model_path,
        )

    def UpdateNavigationStatus(self, nav_status, vis_status):
        if nav_status:
            self.enable_efield.Enable(False)
            self.btn_save.Enable(True)
        else:
            self.enable_efield.Enable(True)
            self.btn_save.Enable(False)

    def OnSelectSleep(self, evt, ctrl):
        self.sleep_nav = ctrl.GetValue()
        # self.tract.seed_offset = ctrl.GetValue()
        Publisher.sendMessage('Update sleep', data=self.sleep_nav)

    def OnGetEfieldActor(self, efield_actor, surface_index_cortex):
        self.e_field_mesh = efield_actor
        self.surface_index= surface_index_cortex
        Publisher.sendMessage('Get Actor', surface_index = self.surface_index)

    def OnGetEfieldPaths(self, path_meshes, cortex_file, meshes_file, coil, ci, co):
        self.path_meshes = path_meshes
        self.cortex_file = cortex_file
        self.meshes_file = meshes_file
        self.ci = ci
        self.co = co
        self.coil = coil

    def OnGetMultilocusCoils(self, multilocus_coil_list):
        self.multilocus_coil = multilocus_coil_list

    def OnSaveEfield(self, evt):
        import invesalius.project as prj

        proj = prj.Project()
        timestamp = time.localtime(time.time())
        stamp_date = '{:0>4d}{:0>2d}{:0>2d}'.format(timestamp.tm_year, timestamp.tm_mon, timestamp.tm_mday)
        stamp_time = '{:0>2d}{:0>2d}{:0>2d}'.format(timestamp.tm_hour, timestamp.tm_min, timestamp.tm_sec)
        sep = '-'
        if self.path_meshes is None:
            import os
            current_folder_path = os.getcwd()
        else:
            current_folder_path = self.path_meshes
        parts = [current_folder_path,'/',stamp_date, stamp_time, proj.name, 'Efield']
        default_filename = sep.join(parts) + '.csv'

        filename = dlg.ShowLoadSaveDialog(message=_(u"Save markers as..."),
                                          wildcard='(*.csv)|*.csv',
                                          style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
                                          default_filename=default_filename)

        if not filename:
            return

        Publisher.sendMessage('Save Efield data', filename = filename)

    def OnSaveAllDataEfield(self, evt):
        Publisher.sendMessage('Check efield data')
        if self.efield_data_saved:
            import invesalius.project as prj
            proj = prj.Project()
            timestamp = time.localtime(time.time())
            stamp_date = '{:0>4d}{:0>2d}{:0>2d}'.format(timestamp.tm_year, timestamp.tm_mon, timestamp.tm_mday)
            stamp_time = '{:0>2d}{:0>2d}{:0>2d}'.format(timestamp.tm_hour, timestamp.tm_min, timestamp.tm_sec)
            sep = '-'
            if self.path_meshes is None:
                import os
                current_folder_path = os.getcwd()
            else:
                current_folder_path = self.path_meshes
            parts = [current_folder_path,'/',stamp_date, stamp_time, proj.name, 'Efield']
            default_filename = sep.join(parts) + '.csv'

            filename = dlg.ShowLoadSaveDialog(message=_(u"Save markers as..."),
                                              wildcard='(*.csv)|*.csv',
                                              style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
                                              default_filename=default_filename)

            if not filename:
                return

            Publisher.sendMessage('Save all Efield data', filename = filename)
        else:
            dlg.Efield_no_data_to_save_warning()

    def SendNeuronavigationApi(self):
        Publisher.sendMessage('Get Neuronavigation Api', neuronavigation_api = self.navigation.neuronavigation_api)

    def GetEfieldDataStatus(self, efield_data_loaded, indexes_saved_list):
        self.efield_data_saved = efield_data_loaded

class SessionPanel(wx.Panel):
    def __init__(self, parent):
        wx.Panel.__init__(self, parent)
        try:
            default_colour = wx.SystemSettings.GetColour(wx.SYS_COLOUR_MENUBAR)
        except AttributeError:
            default_colour = wx.SystemSettings_GetColour(wx.SYS_COLOUR_MENUBAR)
        self.SetBackgroundColour(default_colour)
        
        # session count spinner
        self.__spin_session = wx.SpinCtrl(self, -1, "", size=wx.Size(40, 23))
        self.__spin_session.SetRange(1, 99)
        self.__spin_session.SetValue(1)

        self.__spin_session.Bind(wx.EVT_TEXT, self.OnSessionChanged)
        self.__spin_session.Bind(wx.EVT_SPINCTRL, self.OnSessionChanged)
                
        sizer_create = wx.FlexGridSizer(rows=1, cols=1, hgap=5, vgap=5)
        sizer_create.AddMany([(self.__spin_session, 1)])

    def OnSessionChanged(self, evt):
        Publisher.sendMessage('Current session changed', new_session_id=self.__spin_session.GetValue())
        

class InputAttributes(object):
    # taken from https://stackoverflow.com/questions/2466191/set-attributes-from-dictionary-in-python
    def __init__(self, *initial_data, **kwargs):
        for dictionary in initial_data:
            for key in dictionary:
                setattr(self, key, dictionary[key])
        for key in kwargs:
            setattr(self, key, kwargs[key])
