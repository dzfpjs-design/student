export type ControlledLaunchScope={childId:string;sessionId:string;kind:'video'|'game';requestId:string}
export type ControlledLaunchIntent=ControlledLaunchScope&{intentId:string;activityId:string|null;state:'prepared'|'dispatching'|'closed'|'settled';nativeEpoch?:string}
export type ControlledLaunchReceipt=ControlledLaunchScope&{intentId:string;activityId:string;status:string;nativeEpoch?:string}
export type ControlledLaunchProof={closed:boolean;sessionId?:string;proof?:string;binding?:ControlledLaunchReceipt;noActivity?:boolean}
